import {
  createAgentSession,
  discoverAuthStorage,
  ModelRegistry,
  SessionManager,
  Settings,
  VERSION,
  type AgentSession,
  type AgentSessionEvent,
} from "@oh-my-pi/pi-coding-agent"
import { resolveModelFromString } from "@oh-my-pi/pi-coding-agent/config/model-resolver"

import { EventMapper, normalizeUsage, type StableEvent } from "./event_mapper.js"
import { HistoryMcpBridge, type HistoryDefinition } from "./history_mcp_bridge.js"
import { isRecord, requireString, SCHEMA_VERSION } from "./protocol.js"

type Emit = (
  type: StableEvent["type"],
  payload: Record<string, unknown>,
  context: { requestID: string; sessionID: string; turnID: string },
) => void

export type InitializeResult = {
  sdkVersion: string
  bunVersion: string
  configuredModel: string
  configuredReasoning: string
  permissionPolicy: Record<string, unknown>
  agentDir: string
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
}

/** In-process OHMYPI SDK adapter.  The Python host owns lifecycle and limits. */
export class OmpBackend {
  private session: AgentSession | undefined
  private authStorage: Awaited<ReturnType<typeof discoverAuthStorage>> | undefined
  private modelRegistry: ModelRegistry | undefined
  private historyBridge: HistoryMcpBridge | undefined
  private historyDefinition: HistoryDefinition | undefined
  private historyCallProbeSucceeded = false
  private payload: Record<string, unknown> | undefined
  private turn: BackendState | undefined
  private unsubscribe: (() => void) | undefined
  private emit: Emit

  constructor(emit: Emit) {
    this.emit = emit
  }

  async initialize(payload: Record<string, unknown>): Promise<InitializeResult> {
    if (this.session || this.authStorage) throw new Error("backend is already initialized")
    this.payload = payload
    if (payload.sidecar_protocol_version !== undefined && payload.sidecar_protocol_version !== SCHEMA_VERSION) {
      throw new Error(
        `unsupported sidecar_protocol_version: ${String(payload.sidecar_protocol_version)} (expected ${SCHEMA_VERSION})`,
      )
    }
    const selector = requireString(payload, "model_selector")
    const reasoning = requireString(payload, "reasoning_effort")
    const expectedSdk = requireString(payload, "omp_sdk_version")
    const agentDir = requireString(payload, "agent_dir")
    if (expectedSdk !== VERSION) throw new Error(`OMP SDK version mismatch: expected ${expectedSdk}, got ${VERSION}`)
    if (typeof payload.bun_min_version === "string" && !versionAtLeast(Bun.version, payload.bun_min_version)) {
      throw new Error(`Bun version mismatch: need >= ${payload.bun_min_version}, got ${Bun.version}`)
    }

    this.authStorage = await discoverAuthStorage(agentDir)
    const provider = String(payload.provider ?? selector.split("/", 1)[0])
    if (payload.auth_required !== false && !this.authStorage.hasConcreteAuth(provider)) {
      throw new Error(`AUTH_REQUIRED: provider ${provider} is not logged in for agent directory ${agentDir}`)
    }
    const settings = Settings.isolated({ shellPath: "/app/bin/bash" })
    this.modelRegistry = new ModelRegistry(this.authStorage, `${agentDir}/models.yml`, {
      settings,
      cacheDbPath: `${agentDir}/models.db`,
    })
    await this.modelRegistry.hydrateCredentialScopedModelCaches()
    const model = resolveModelFromString(selector, this.modelRegistry.getAll())
    if (!model) throw new Error(`MODEL_MISMATCH: model selector is unavailable: ${selector}`)

    const historyEnabled = payload.history_enabled === true
    let customTools: any[] = []
    if (historyEnabled) {
      this.historyBridge = new HistoryMcpBridge()
      this.historyDefinition = await this.historyBridge.start(payload)
      const probeArguments = payload.mcp_probe_arguments
      if (!isRecord(probeArguments)) throw new Error("history MCP call probe arguments are missing")
      const probeResult = await this.historyBridge.call(probeArguments)
      if (probeResult.isError) throw new Error("history MCP call probe returned an error")
      this.historyCallProbeSucceeded = true
      customTools = [this.historyBridge.customTool()]
    }
    const enabledTools = stringArray(payload.enabled_tools)
    const disabledTools = stringArray(payload.disabled_tools)
    const forbiddenEnabled = ["edit", "write", "apply_patch", "patch", "task", "subagent", "lsp", "browser", "skill", "memory"]
      .filter((name) => enabledTools.includes(name))
    if (forbiddenEnabled.length) throw new Error(`forbidden tools are enabled: ${forbiddenEnabled.join(", ")}`)
    if (enabledTools.some((name) => disabledTools.includes(name))) throw new Error("tools cannot be enabled and disabled")

    const created = await createAgentSession({
      cwd: "/workspace/source",
      agentDir,
      authStorage: this.authStorage,
      modelRegistry: this.modelRegistry,
      model,
      thinkingLevel: reasoning as any,
      settings,
      sessionManager: SessionManager.inMemory("/workspace/source"),
      customTools,
      toolNames: enabledTools,
      restrictToolNames: true,
      allowRestrictedCustomTools: true,
      disableExtensionDiscovery: true,
      enableMCP: false,
      enableLsp: false,
      enableIrc: false,
      spawns: "",
      skills: [],
      rules: [],
      contextFiles: [],
      promptTemplates: [],
      slashCommands: [],
      hasUI: false,
      interactivePrompts: false,
      autoApprove: true,
    })
    if (created.modelFallbackMessage) throw new Error(`MODEL_MISMATCH: ${created.modelFallbackMessage}`)
    this.session = created.session
    this.unsubscribe = this.session.subscribe((event) => this.handleEvent(event))
    return {
      sdkVersion: VERSION,
      bunVersion: Bun.version,
      configuredModel: selector,
      configuredReasoning: reasoning,
      permissionPolicy: {
        autoApprove: true,
        restrictedTools: true,
        disabledTools,
        extensions: "disabled",
        mcp: "bridge-only",
        lsp: "disabled",
        shellPath: "/app/bin/bash",
      },
      agentDir,
    }
  }

  async startSession(): Promise<SessionResult> {
    const session = this.requireSession()
    const effectiveTools = session.getEnabledToolNames().sort()
    const historyEnabled = this.payload?.history_enabled === true
    const mcp: Record<string, unknown> = historyEnabled
      ? {
          server: "patchhound_history",
          status: this.historyDefinition ? "connected" : "error",
          error: this.historyDefinition ? null : "history bridge unavailable",
          tool: "query_pre_patch_history",
          schema_sha256: this.historyDefinition?.schemaSha256 ?? null,
          call_probe: this.historyCallProbeSucceeded ? "succeeded" : "failed",
          stderr_summary: this.historyBridge?.stderrSummary || null,
        }
      : {
          server: "patchhound_history",
          status: "disabled",
          error: null,
          tool: "query_pre_patch_history",
          schema_sha256: null,
          call_probe: "disabled",
          stderr_summary: null,
        }
    if (historyEnabled && !effectiveTools.includes("query_pre_patch_history")) {
      throw new Error("required history MCP tool is not in the effective tool set")
    }
    return { sessionID: session.sessionId, mcp, effectiveTools }
  }

  async prompt(requestID: string, name: string, text: string, turnID: string): Promise<PromptResult> {
    const session = this.requireSession()
    if (this.turn) throw new Error("a turn is already active")
    const mapper = new EventMapper()
    this.turn = { requestID, turnID, mapper }
    try {
      const accepted = await session.prompt(text, { expandPromptTemplates: false, userInitiated: true })
      if (!accepted) throw new Error(`OMP prompt was handled without a model turn: ${name}`)
      await session.waitForIdle()
      const message = session.getLastAssistantMessage()
      if (!message) throw new Error("OMP session ended without an assistant message")
      const finalResponse = message.content
        .filter((part: any) => part && part.type === "text")
        .map((part: any) => (typeof part.text === "string" ? part.text : ""))
        .join("")
      const configuredReasoning = typeof this.payload?.reasoning_effort === "string" ? this.payload.reasoning_effort : null
      return {
        turnID,
        finalResponse,
        finishReason: typeof message.stopReason === "string" ? message.stopReason : null,
        provider: typeof message.provider === "string" ? message.provider : null,
        model: typeof message.model === "string" ? message.model : null,
        reasoningEffort: typeof session.thinkingLevel === "string" ? session.thinkingLevel : configuredReasoning,
        usage: normalizeUsage(message.usage),
        contextCompacted: mapper.contextCompacted,
        sdkRetryCount: mapper.sdkRetryCount ?? null,
      }
    } finally {
      this.turn = undefined
    }
  }

  async abort(): Promise<void> {
    await this.session?.abort({ goalReason: "internal" }).catch(() => undefined)
  }

  async dispose(): Promise<void> {
    this.unsubscribe?.()
    this.unsubscribe = undefined
    await this.session?.dispose().catch(() => undefined)
    this.session = undefined
    await this.historyBridge?.close().catch(() => undefined)
    this.historyBridge = undefined
    this.historyDefinition = undefined
    this.historyCallProbeSucceeded = false
    this.modelRegistry = undefined
    this.authStorage?.close()
    this.authStorage = undefined
    this.turn = undefined
  }

  private requireSession(): AgentSession {
    if (!this.session) throw new Error("backend is not initialized")
    return this.session
  }

  private handleEvent(event: AgentSessionEvent): void {
    const turn = this.turn
    if (!turn) return
    const mapped = turn.mapper.map(event)
    for (const item of mapped) {
      this.emit(item.type, item.payload, {
        requestID: turn.requestID,
        sessionID: this.requireSession().sessionId,
        turnID: turn.turnID,
      })
    }
  }
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : []
}

function versionAtLeast(actual: string, minimum: string): boolean {
  const parse = (value: string) => value.replace(/^v/, "").split(".").map((part) => Number.parseInt(part, 10) || 0)
  const a = parse(actual)
  const b = parse(minimum)
  for (let index = 0; index < 3; index++) {
    if ((a[index] ?? 0) !== (b[index] ?? 0)) return (a[index] ?? 0) > (b[index] ?? 0)
  }
  return true
}
