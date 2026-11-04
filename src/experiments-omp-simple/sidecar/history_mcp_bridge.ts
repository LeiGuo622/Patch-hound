import { createHash } from "node:crypto"
import { createInterface, type Interface } from "node:readline"
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { existsSync } from "node:fs"
import { fromJsonSchema } from "@oh-my-pi/omptype"

export const MCP_PROTOCOL_VERSION = "2025-06-18"
export const MCP_SERVER_NAME = "patchhound_history"
export const MCP_TOOL_NAME = "query_pre_patch_history"

type JsonObject = Record<string, unknown>
type Pending = { resolve: (value: JsonObject) => void; reject: (reason: unknown) => void; timer: ReturnType<typeof setTimeout> }

export type HistoryDefinition = {
  serverName: string
  toolName: string
  schema: JsonObject
  schemaSha256: string
}

export class HistoryMcpBridge {
  private child: ChildProcessWithoutNullStreams | undefined
  private reader: Interface | undefined
  private nextID = 1
  private pending = new Map<number, Pending>()
  private definition: HistoryDefinition | undefined
  private stderr = ""
  private timeoutMs = 30_000

  get schemaSha256(): string | undefined {
    return this.definition?.schemaSha256
  }

  get toolName(): string {
    return MCP_TOOL_NAME
  }

  get stderrSummary(): string {
    return this.stderr.slice(-8192)
  }

  async start(payload: Record<string, unknown>): Promise<HistoryDefinition> {
    if (this.child) throw new Error("history MCP bridge is already started")
    const command = stringArray(payload.mcp_command)
    if (command.length < 1) throw new Error("mcp_command must not be empty")
    this.timeoutMs = positiveInteger(payload.mcp_timeout_ms, 30_000)
    const env = {
      ...process.env,
      PATCHHOUND_CASE_ID: stringValue(payload.case_id),
      PATCHHOUND_PROJECT_ROOT: stringValue(payload.project_root),
      PATCHHOUND_METADATA_DIR: stringValue(payload.metadata_dir),
      PATCHHOUND_LINUX_REPO: stringValue(payload.linux_repo),
      PATCHHOUND_HISTORY_QUERY_LOG: stringValue(payload.query_log),
    }
    this.child = spawn(command[0], command.slice(1), {
      cwd: existsSync("/workspace/source") ? "/workspace/source" : process.cwd(),
      env,
      stdio: "pipe",
    })
    this.reader = createInterface({ input: this.child.stdout, crlfDelay: Infinity })
    this.reader.on("line", (line) => this.handleLine(line))
    this.child.stderr.on("data", (chunk: Buffer) => {
      this.stderr = (this.stderr + chunk.toString("utf8")).slice(-8192)
    })
    this.child.on("error", (error) => this.failAll(error))
    this.child.on("exit", (code, signal) => {
      if (code !== 0 || signal) this.failAll(new Error(`history MCP exited code=${code} signal=${signal}`))
    })
    const initialized = await this.rpc("initialize", {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "patchhound-omp-simple", version: "0.1.0" },
    })
    const serverInfo = isRecord(initialized.serverInfo) ? initialized.serverInfo : {}
    if (serverInfo.name !== MCP_SERVER_NAME) throw new Error(`unexpected history MCP server: ${String(serverInfo.name)}`)
    this.notify("notifications/initialized", {})
    const listed = await this.rpc("tools/list", {})
    const tools = Array.isArray(listed.tools) ? listed.tools : []
    if (tools.length !== 1 || !isRecord(tools[0]) || tools[0].name !== MCP_TOOL_NAME) {
      throw new Error("history MCP must expose exactly query_pre_patch_history")
    }
    const schema = isRecord(tools[0].inputSchema) ? tools[0].inputSchema : undefined
    if (!schema) throw new Error("history MCP tool has no input schema")
    this.definition = {
      serverName: MCP_SERVER_NAME,
      toolName: MCP_TOOL_NAME,
      schema,
      schemaSha256: sha256Canonical(schema),
    }
    const expected = typeof payload.mcp_schema_sha256 === "string" ? payload.mcp_schema_sha256 : undefined
    if (expected && expected !== this.definition.schemaSha256) {
      throw new Error(`history MCP schema mismatch: expected ${expected}, got ${this.definition.schemaSha256}`)
    }
    return this.definition
  }

  async call(argumentsValue: Record<string, unknown>, signal?: AbortSignal): Promise<{ text: string; isError: boolean }> {
    const result = await this.rpc("tools/call", { name: MCP_TOOL_NAME, arguments: argumentsValue }, signal)
    const content = Array.isArray(result.content) ? result.content : []
    const text = content
      .filter((item): item is JsonObject => isRecord(item) && item.type === "text")
      .map((item) => (typeof item.text === "string" ? item.text : ""))
      .join("\n")
    return { text, isError: result.isError === true }
  }

  customTool(): any {
    const schema = this.definition?.schema
    if (!schema) throw new Error("history MCP bridge is not initialized")
    const bridge = this
    return {
      name: MCP_TOOL_NAME,
      label: MCP_TOOL_NAME,
      description: "Query sanitized pre-patch history for a file, function, or symbol.",
      parameters: fromJsonSchema(schema) as any,
      strict: true,
      hidden: false,
      loadMode: "essential",
      approval: "read",
      async execute(_toolCallID: string, params: Record<string, unknown>, _onUpdate: unknown, _ctx: unknown, signal?: AbortSignal) {
        const result = await bridge.call(params, signal)
        return { content: [{ type: "text", text: result.text }], isError: result.isError }
      },
    }
  }

  async close(): Promise<void> {
    this.reader?.close()
    this.reader = undefined
    const child = this.child
    this.child = undefined
    this.failAll(new Error("history MCP bridge closed"))
    if (!child) return
    child.stdin.end()
    await new Promise<void>((resolve) => {
      let finished = false
      let termTimer: ReturnType<typeof setTimeout> | undefined
      let killTimer: ReturnType<typeof setTimeout> | undefined
      const finish = () => {
        if (finished) return
        finished = true
        if (termTimer) clearTimeout(termTimer)
        if (killTimer) clearTimeout(killTimer)
        resolve()
      }
      child.once("exit", finish)
      termTimer = setTimeout(() => {
        child.kill("SIGTERM")
        killTimer = setTimeout(() => {
          child.kill("SIGKILL")
          killTimer = setTimeout(finish, 1000)
        }, 1000)
      }, 1000)
    })
  }

  private rpc(method: string, params: JsonObject, signal?: AbortSignal): Promise<JsonObject> {
    const child = this.child
    if (!child) return Promise.reject(new Error("history MCP bridge is not running"))
    const id = this.nextID++
    const request = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n"
    return new Promise<JsonObject>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`history MCP timeout: ${method}`))
      }, this.timeoutMs)
      const abort = () => {
        clearTimeout(timer)
        this.pending.delete(id)
        reject(new Error(`history MCP call aborted: ${method}`))
      }
      if (signal?.aborted) {
        abort()
        return
      }
      signal?.addEventListener("abort", abort, { once: true })
      this.pending.set(id, {
        resolve: (value) => {
          signal?.removeEventListener("abort", abort)
          clearTimeout(timer)
          resolve(value)
        },
        reject: (reason) => {
          signal?.removeEventListener("abort", abort)
          clearTimeout(timer)
          reject(reason)
        },
        timer,
      })
      child.stdin.write(request)
    })
  }

  private notify(method: string, params: JsonObject): void {
    this.child?.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n")
  }

  private handleLine(line: string): void {
    let value: unknown
    try {
      value = JSON.parse(line)
    } catch {
      return
    }
    if (!isRecord(value) || typeof value.id !== "number") return
    const pending = this.pending.get(value.id)
    if (!pending) return
    this.pending.delete(value.id)
    if (isRecord(value.error)) pending.reject(new Error(String(value.error.message ?? "MCP error")))
    else if (isRecord(value.result)) pending.resolve(value.result)
    else pending.reject(new Error("invalid history MCP response"))
  }

  private failAll(error: Error): void {
    for (const [id, pending] of this.pending) {
      this.pending.delete(id)
      clearTimeout(pending.timer)
      pending.reject(error)
    }
  }
}

export function sha256Canonical(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex")
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (isRecord(value)) return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]))
  return value
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : []
}

function stringValue(value: unknown): string {
  if (typeof value !== "string" || !value) throw new Error("history MCP environment value is missing")
  return value
}

function positiveInteger(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : fallback
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}
