import { createHash } from "node:crypto"
import { readFile } from "node:fs/promises"

import { Agent } from "undici"

import { createOpencodeClient, createOpencodeServer } from "@opencode-ai/sdk/v2"
import type { Config } from "@opencode-ai/sdk/v2/types"

import { EventMapper, normalizeUsage, type StableEvent } from "./event_mapper.js"
import { isRecord, requireString, SCHEMA_VERSION } from "./protocol.js"

type Emit = (
  type: StableEvent["type"],
  payload: Record<string, unknown>,
  context: { requestID: string; sessionID: string; turnID: string },
) => void

export type InitializeResult = {
  serverVersion: string
  sdkVersion: string
  cliVersion: string
  configuredModel: string
  configuredReasoning: string
  permissionPolicy: Config["permission"]
}

export type SessionResult = {
  sessionID: string
  mcp: Record<string, unknown>
  effectiveTools: string[]
}

export type PromptResult = {
  turnID: string
  finalResponse: string
  finishReason: string | null
  provider: string | null
  model: string | null
  reasoningEffort: string | null
  usage: Record<string, unknown>
  contextCompacted: boolean
  sdkRetryCount: number | null
}

type BackendState = {
  requestID: string
  turnID: string
  mapper: EventMapper
  resolve: (result: PromptResult) => void
  reject: (error: Error) => void
}

type OpenCodeInstance = {
  server: Awaited<ReturnType<typeof createOpencodeServer>>
  client: ReturnType<typeof createOpencodeClient>
}

export class OpenCodeBackend {
  private opencode: OpenCodeInstance | undefined
  private client: OpenCodeInstance["client"] | undefined
  private httpAgent: Agent | undefined
  private sessionID: string | undefined
  private eventAbort = new AbortController()
  private eventTask: Promise<void> | undefined
  private turn: BackendState | undefined
  private payload: Record<string, unknown> | undefined
  private emit: Emit
  private eventError: Error | undefined

  constructor(emit: Emit) {
    this.emit = emit
  }

  async initialize(payload: Record<string, unknown>): Promise<InitializeResult> {
    if (this.opencode) throw new Error("backend is already initialized")
    this.payload = payload
    if (payload.sidecar_protocol_version !== undefined && payload.sidecar_protocol_version !== SCHEMA_VERSION) {
      throw new Error(
        `unsupported sidecar_protocol_version: ${String(payload.sidecar_protocol_version)} (expected ${SCHEMA_VERSION})`,
      )
    }
    const modelSelector = requireString(payload, "model_selector")
    const reasoning = requireString(payload, "reasoning_effort")
    const expectedVersion = requireString(payload, "expected_version")
    const startTimeout = number(payload.server_ready_timeout_seconds, 15)
    const permission = permissionPolicy(payload)
    const config = buildConfig(payload, permission)
    const providerTimeoutMs = positiveNumber(payload.provider_timeout_ms, 3_600_000)
    const server = await createOpencodeServer({
      hostname: "127.0.0.1",
      port: 0,
      timeout: startTimeout * 1000,
      signal: this.eventAbort.signal,
      config,
    })
    // Node's built-in fetch uses Undici's 300s headersTimeout. The OpenCode
    // SDK disables Bun's Request timeout, but that does not affect Node's
    // dispatcher. Inject an explicit Agent so long-running turns share the
    // host's deadline regardless of the runtime used to launch the sidecar.
    this.httpAgent = new Agent({ headersTimeout: providerTimeoutMs, bodyTimeout: providerTimeoutMs })
    const fetchWithAgent: typeof fetch = (input, init) =>
      fetch(input, { ...init, dispatcher: this.httpAgent } as RequestInit & { dispatcher: Agent })
    this.client = createOpencodeClient({ baseUrl: server.url, fetch: fetchWithAgent })
    this.opencode = { server, client: this.client }
    const health = unwrap<Record<string, unknown>>(await this.client.global.health())
    const serverVersion = requireString(health, "version")
    const sdkVersion = await packageVersion("/app/node_modules/@opencode-ai/sdk/package.json")
    const cliVersion = await packageVersion("/app/node_modules/opencode-ai/package.json")
    for (const [name, value] of Object.entries({ serverVersion, sdkVersion, cliVersion })) {
      if (value !== expectedVersion) throw new Error(`${name} mismatch: expected ${expectedVersion}, got ${value}`)
    }
    await this.startEventStream()
    return {
      serverVersion,
      sdkVersion,
      cliVersion,
      configuredModel: modelSelector,
      configuredReasoning: reasoning,
      permissionPolicy: permission,
    }
  }

  async startSession(): Promise<SessionResult> {
    const client = this.requireClient()
    const payload = this.requirePayload()
    const selector = splitModel(requireString(payload, "model_selector"))
    const response = unwrap<Record<string, unknown>>(
      await client.session.create({
        directory: "/workspace/source",
        title: `PatchHound ${String(payload.case_id ?? "case")}`,
        agent: "build",
        model: { id: selector.modelID, providerID: selector.providerID, variant: requireString(payload, "reasoning_effort") },
      }),
    )
    this.sessionID = requireString(response, "id")
    const historyEnabled = payload.history_enabled === true
    const listed = unwrap<unknown[]>(
      await client.tool.list({
        directory: "/workspace/source",
        provider: selector.providerID,
        model: selector.modelID,
      }),
    )
    const availableTools = new Set(
      listed.filter(isRecord).map((item) => item.id).filter((id): id is string => typeof id === "string"),
    )
    const enabled = stringArray(payload.enabled_tools)
    // `/experimental/tool` reports provider-native tools only. MCP tools are
    // registered dynamically and are exposed through the MCP status endpoint
    // instead, so validate the namespaced history tool against that status
    // rather than treating it as a missing provider tool.
    const mcpStatus = unwrap<Record<string, unknown>>(await client.mcp.status({ directory: "/workspace/source" }))
    const historyStatus = mcpStatus.patchhound_history
    const historyConnected = isRecord(historyStatus) && historyStatus.status === "connected"
    const missingTools = enabled.filter(
      (tool) => !availableTools.has(tool) && !(historyEnabled && tool === "patchhound_history_query_pre_patch_history" && historyConnected),
    )
    if (missingTools.length) {
      throw new Error(
        `configured tools are not available: ${missingTools.join(", ")}; available=${JSON.stringify([...availableTools].sort())}; mcp=${JSON.stringify(mcpStatus)}`,
      )
    }
    if (!historyEnabled) {
      return {
        sessionID: this.sessionID,
        mcp: {
          server: "patchhound_history",
          status: "disabled",
          error: null,
          tool: "patchhound_history_query_pre_patch_history",
          schema_sha256: null,
          call_probe: "disabled",
        },
        effectiveTools: enabled.sort(),
      }
    }
    if (!historyConnected) throw new Error(`required history MCP is not connected: ${JSON.stringify(historyStatus)}`)
    // The host-side direct MCP probe independently hashes the exact schema
    // before starting the broker. OpenCode's tool-list endpoint intentionally
    // excludes dynamic MCP tools, so carry that verified hash through here.
    const schemaSha256 = requireString(payload, "mcp_schema_sha256")
    const mcp = {
      server: "patchhound_history",
      status: "connected",
      error: isRecord(historyStatus) && typeof historyStatus.error === "string" ? historyStatus.error : null,
      tool: "patchhound_history_query_pre_patch_history",
      schema_sha256: schemaSha256,
      call_probe: "succeeded",
    }
    return { sessionID: this.sessionID, mcp, effectiveTools: enabled.sort() }
  }

  async prompt(requestID: string, name: string, text: string, turnID: string): Promise<PromptResult> {
    const client = this.requireClient()
    const payload = this.requirePayload()
    const sessionID = this.requireSessionID()
    if (this.turn) throw new Error("a turn is already active")
    if (this.eventError) throw this.eventError
    const mapper = new EventMapper()
    let resolveTurn!: (result: PromptResult) => void
    let rejectTurn!: (error: Error) => void
    const completed = new Promise<PromptResult>((resolve, reject) => {
      resolveTurn = resolve
      rejectTurn = reject
    })
    this.turn = { requestID, turnID, mapper, resolve: resolveTurn, reject: rejectTurn }
    try {
      const selector = splitModel(requireString(payload, "model_selector"))
      // Use the async endpoint so the Node SDK does not hold an HTTP response
      // open for the full agent turn (Node/Undici otherwise has a 300s header
      // timeout). Completion and tool events arrive through the event stream.
      unwrap<Record<string, unknown>>(
        await client.session.promptAsync({
          sessionID,
          directory: "/workspace/source",
          agent: "build",
          model: { providerID: selector.providerID, modelID: selector.modelID },
          variant: requireString(payload, "reasoning_effort"),
          tools: toolSwitches(payload),
          parts: [{ type: "text", text }],
        }),
      )
      if (this.eventError) throw this.eventError
      return await completed
    } catch (error) {
      const failure = error instanceof Error ? error : new Error(String(error))
      rejectTurn(failure)
      throw failure
    } finally {
      this.turn = undefined
    }
  }

  async abort(): Promise<void> {
    if (this.client && this.sessionID) {
      await this.client.session.abort({ sessionID: this.sessionID, directory: "/workspace/source" })
    }
  }

  async dispose(): Promise<void> {
    this.eventAbort.abort()
    if (this.client && this.sessionID) {
      await this.client.session.delete({ sessionID: this.sessionID, directory: "/workspace/source" }).catch(() => undefined)
    }
    this.opencode?.server.close()
    await this.eventTask?.catch(() => undefined)
    this.client = undefined
    this.opencode = undefined
    await this.httpAgent?.close().catch(() => undefined)
    this.httpAgent = undefined
    this.sessionID = undefined
  }

  private async startEventStream(): Promise<void> {
    const client = this.requireClient()
    const subscription = await client.event.subscribe(
      { directory: "/workspace/source" },
      { signal: this.eventAbort.signal },
    )
    this.eventTask = (async () => {
      try {
        for await (const event of subscription.stream) {
          const rawEvent: unknown = event
          const properties = isRecord(rawEvent) && isRecord(rawEvent.properties) ? rawEvent.properties : {}
          const eventSession = typeof properties.sessionID === "string" ? properties.sessionID : undefined
          if (eventSession && this.sessionID && eventSession !== this.sessionID) continue
          const turn = this.turn
          if (isRecord(rawEvent) && rawEvent.type === "session.idle" && turn) {
            try {
              turn.resolve(await this.completedTurn(turn))
            } catch (error) {
              turn.reject(error instanceof Error ? error : new Error(String(error)))
            }
            continue
          }
          const mapped = this.turn?.mapper.map(event)
          if (!mapped) continue
          if (mapped.permission) {
            await client.permission.reply({
              requestID: mapped.permission.id,
              directory: "/workspace/source",
              reply: "reject",
              message: "PatchHound rejects unexpected permission requests",
            })
          }
          if (mapped.error) {
            this.eventError = new Error(`OpenCode session error: ${JSON.stringify(mapped.error)}`)
            turn?.reject(this.eventError)
          }
          if (!turn) continue
          for (const item of mapped.events) {
            this.emit(item.type, item.payload, {
              requestID: turn.requestID,
              sessionID: this.requireSessionID(),
              turnID: turn.turnID,
            })
          }
        }
      } catch (error) {
        if (!this.eventAbort.signal.aborted) this.eventError = error instanceof Error ? error : new Error(String(error))
      }
    })()
  }

  private requireClient(): NonNullable<OpenCodeBackend["client"]> {
    if (!this.client) throw new Error("backend is not initialized")
    return this.client
  }

  private requirePayload(): Record<string, unknown> {
    if (!this.payload) throw new Error("backend is not initialized")
    return this.payload
  }

  private requireSessionID(): string {
    if (!this.sessionID) throw new Error("session has not been started")
    return this.sessionID
  }

  private async completedTurn(turn: BackendState): Promise<PromptResult> {
    const client = this.requireClient()
    const sessionID = this.requireSessionID()
    const messages = unwrap<unknown[]>(await client.session.messages({ sessionID, directory: "/workspace/source" }))
    const assistant = [...messages].reverse().find((item) => {
      if (!isRecord(item) || !isRecord(item.info)) return false
      return item.info.role === "assistant"
    })
    if (!isRecord(assistant)) throw new Error("OpenCode session became idle without an assistant response")
    const info = isRecord(assistant.info) ? assistant.info : {}
    if (isRecord(info.error)) throw new Error(`OpenCode turn failed: ${JSON.stringify(info.error)}`)
    const parts = Array.isArray(assistant.parts) ? assistant.parts : []
    const finalResponse = parts
      .filter((part): part is Record<string, unknown> => isRecord(part) && part.type === "text")
      .map((part) => (typeof part.text === "string" ? part.text : ""))
      .join("")
    return {
      turnID: turn.turnID,
      finalResponse,
      finishReason: typeof info.finish === "string" ? info.finish : null,
      provider: typeof info.providerID === "string" ? info.providerID : null,
      model: typeof info.modelID === "string" ? info.modelID : null,
      reasoningEffort: typeof info.variant === "string" ? info.variant : null,
      usage: isRecord(info.tokens) ? normalizeUsage(info.tokens) : emptyUsage(),
      contextCompacted: turn.mapper.contextCompacted,
      sdkRetryCount: turn.mapper.sdkRetryCount ?? null,
    }
  }
}

export function buildConfig(payload: Record<string, unknown>, permission: NonNullable<Config["permission"]>): Config {
  const selector = requireString(payload, "model_selector")
  const model = splitModel(selector)
  const reasoning = requireString(payload, "reasoning_effort")
  const providerTimeoutMs = positiveNumber(payload.provider_timeout_ms, 3_600_000)
  const historyEnabled = payload.history_enabled === true
  const mcpCommand = stringArray(payload.mcp_command)
  if (historyEnabled && !mcpCommand.length) throw new Error("mcp_command must not be empty")
  const providerConfig =
    model.providerID === "dgx"
      ? {
          // The DGX vLLM Responses endpoint strictly validates replayed input
          // items and rejects output_text parts in message history, so route
          // this provider through the chat-completions wire API instead.
          npm: "@ai-sdk/openai-compatible",
          name: "DGX Spark DeepSeek",
          options: {
            baseURL: typeof payload.provider_base_url === "string" && payload.provider_base_url ? payload.provider_base_url : "http://127.0.0.1:8000/v1",
            apiKey: "{env:PATCHHOUND_DGX_API_KEY}",
            timeout: providerTimeoutMs,
            headerTimeout: providerTimeoutMs,
          },
          models: {
            [model.modelID]: {
              name: model.modelID,
              limit: { context: 256_000, output: 32_768 },
            },
          },
        }
      : model.providerID === "newapi"
      ? {
          npm: "@ai-sdk/openai",
          name: "NewAPI DeepSeek",
          options: {
            baseURL: typeof payload.provider_base_url === "string" && payload.provider_base_url ? payload.provider_base_url : "https://api.example.invalid/v1",
            apiKey: "{env:PATCHHOUND_DEEPSEEK_API_KEY}",
            timeout: providerTimeoutMs,
            headerTimeout: providerTimeoutMs,
          },
          models: {
            [model.modelID]: {
              name: model.modelID,
              limit: { context: 256_000, output: 32_768 },
            },
          },
        }
      : model.providerID === "zhipu-coding-plan"
        ? {
            npm: "@ai-sdk/openai-compatible",
            name: "Zhipu AI Coding Plan",
            options: {
              baseURL: typeof payload.provider_base_url === "string" && payload.provider_base_url ? payload.provider_base_url : "https://open.bigmodel.cn/api/coding/paas/v4",
              apiKey: "{env:PATCHHOUND_GLM_CODING_API_KEY}",
              timeout: providerTimeoutMs,
              headerTimeout: providerTimeoutMs,
            },
            models: {
              [model.modelID]: {
                name: model.modelID,
                limit: { context: 256_000, output: 32_768 },
              },
            },
          }
      : {
          options: {
            timeout: providerTimeoutMs,
            // OpenCode's built-in OpenAI loader also applies a 300s header
            // timeout; override it for long-running Responses requests.
            headerTimeout: providerTimeoutMs,
          },
        }
  return {
    model: selector,
    // OpenCode's provider default is five minutes. Keep the SDK's full
    // request timeout aligned with the host's total run deadline so a long
    // analysis is not aborted at the provider layer before the host deadline.
    provider: {
      [model.providerID]: providerConfig,
    },
    default_agent: "build",
    share: "disabled",
    autoupdate: false,
    snapshot: false,
    shell: "/app/bin/bash",
    lsp: false,
    formatter: false,
    plugin: [],
    instructions: [],
    skills: { paths: [], urls: [] },
    subagent_depth: 0,
    tools: toolSwitches(payload),
    permission,
    agent: {
      build: {
        mode: "primary",
        model: selector,
        variant: reasoning,
        tools: toolSwitches(payload),
        permission,
      },
      plan: { disable: true },
      general: { disable: true },
      explore: { disable: true },
    },
    mcp: historyEnabled
      ? {
          patchhound_history: {
            type: "local",
            command: mcpCommand,
            enabled: true,
            timeout: number(payload.mcp_timeout_ms, 30_000),
          },
        }
      : {},
    tool_output: {
      max_bytes: number(payload.tool_output_bytes, 131_072),
      max_lines: 5_000,
    },
    compaction: { auto: true, prune: true },
  }
}

export function permissionPolicy(payload: Record<string, unknown>): NonNullable<Config["permission"]> {
  const result: Record<string, unknown> = { "*": "deny", external_directory: "deny" }
  for (const tool of stringArray(payload.enabled_tools)) result[tool] = "allow"
  for (const tool of stringArray(payload.disabled_tools)) result[tool] = "deny"
  return result as NonNullable<Config["permission"]>
}

export function toolSwitches(payload: Record<string, unknown>): Record<string, boolean> {
  const result: Record<string, boolean> = {}
  for (const tool of stringArray(payload.enabled_tools)) result[tool] = true
  for (const tool of stringArray(payload.disabled_tools)) result[tool] = false
  return result
}

function splitModel(selector: string): { providerID: string; modelID: string } {
  const index = selector.indexOf("/")
  if (index <= 0 || index === selector.length - 1) throw new Error(`invalid model selector: ${selector}`)
  return { providerID: selector.slice(0, index), modelID: selector.slice(index + 1) }
}

function unwrap<T>(response: unknown): T {
  if (!isRecord(response)) throw new Error("SDK returned a non-object response")
  if (response.error !== undefined) {
    const status = isRecord(response.response) && typeof response.response.status === "number" ? ` (HTTP ${response.response.status})` : ""
    throw new Error(`SDK request failed${status}: ${describeSdkError(response.error)}`)
  }
  if (response.data === undefined) throw new Error("SDK response did not contain data")
  return response.data as T
}

/** Preserve useful network/transport details instead of JSON.stringify(Error) => "{}". */
export function describeSdkError(error: unknown): string {
  if (error instanceof Error) {
    const cause = isRecord(error.cause) && typeof error.cause.code === "string" ? ` (code ${error.cause.code})` : ""
    return `${error.name}: ${error.message || "(no message)"}${cause}`
  }
  if (typeof error === "string") return error
  if (isRecord(error) && typeof error.message === "string") return error.message
  try {
    return JSON.stringify(error)
  } catch {
    return String(error)
  }
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) return []
  return [...value]
}

function number(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback
}

function positiveNumber(value: unknown, fallback: number): number {
  const result = number(value, fallback)
  if (result <= 0) throw new Error("provider_timeout_ms must be positive")
  return result
}

async function packageVersion(path: string): Promise<string> {
  const value: unknown = JSON.parse(await readFile(path, "utf8"))
  if (!isRecord(value)) throw new Error(`invalid package metadata: ${path}`)
  return requireString(value, "version")
}

function emptyUsage(): Record<string, unknown> {
  return {
    input: null,
    output: null,
    reasoning: null,
    cache_read: null,
    cache_write: null,
    total: null,
    raw: {},
  }
}

export function sha256Canonical(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex")
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => canonical(item))
  if (!isRecord(value)) return value
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((key) => [key, canonical(value[key])]),
  )
}
