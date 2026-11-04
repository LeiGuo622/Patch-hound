import { createHash } from "node:crypto"

import { isRecord } from "./protocol.js"

export type StableEvent = {
  type:
    | "assistant_delta"
    | "reasoning_delta"
    | "tool_started"
    | "tool_updated"
    | "tool_finished"
    | "token_usage"
    | "permission_denied"
  payload: Record<string, unknown>
}

type PermissionRequest = { id: string; permission: string; patterns: string[] }

export class EventMapper {
  private readonly partKinds = new Map<string, string>()
  private readonly toolStates = new Map<string, string>()
  contextCompacted = false
  sdkRetryCount: number | undefined

  map(event: unknown): { events: StableEvent[]; permission?: PermissionRequest; error?: Record<string, unknown> } {
    if (!isRecord(event) || typeof event.type !== "string" || !isRecord(event.properties)) return { events: [] }
    const properties = event.properties
    const v2 = this.mapV2(event.type, properties)
    if (v2 !== undefined) return v2
    if (event.type === "message.part.delta") return { events: this.mapDelta(properties) }
    if (event.type === "message.part.updated") return { events: this.mapPart(properties.part) }
    if (event.type === "message.updated") return { events: this.mapMessage(properties.info) }
    if (event.type === "session.compacted") {
      this.contextCompacted = true
      return { events: [] }
    }
    if (event.type === "permission.asked") {
      const id = typeof properties.id === "string" ? properties.id : "unknown"
      const permission = typeof properties.permission === "string" ? properties.permission : "unknown"
      const patterns = Array.isArray(properties.patterns)
        ? properties.patterns.filter((item): item is string => typeof item === "string")
        : []
      return {
        events: [{ type: "permission_denied", payload: { permission, patterns } }],
        permission: { id, permission, patterns },
      }
    }
    if (event.type === "session.error") {
      return { events: [], error: isRecord(properties.error) ? properties.error : { message: "unknown session error" } }
    }
    return { events: [] }
  }

  private mapV2(type: string, properties: Record<string, unknown>): { events: StableEvent[]; error?: Record<string, unknown> } | undefined {
    if (type === "session.next.text.delta") {
      const delta = typeof properties.delta === "string" ? properties.delta : ""
      return { events: delta ? [{ type: "assistant_delta", payload: { text: delta } }] : [] }
    }
    if (type === "session.next.reasoning.delta") {
      const delta = typeof properties.delta === "string" ? properties.delta : ""
      return {
        events: delta
          ? [{ type: "reasoning_delta", payload: { chars: delta.length, bytes: Buffer.byteLength(delta) } }]
          : [],
      }
    }
    if (type === "session.next.tool.called") {
      return { events: [this.v2ToolEvent(properties, "started")] }
    }
    if (type === "session.next.tool.progress") {
      return { events: [this.v2ToolEvent(properties, "updated")] }
    }
    if (type === "session.next.tool.success") {
      return { events: [this.v2ToolEvent(properties, "finished", true)] }
    }
    if (type === "session.next.tool.failed") {
      return { events: [this.v2ToolEvent(properties, "finished", false)] }
    }
    if (type === "session.next.retried") {
      if (typeof properties.attempt === "number" && Number.isInteger(properties.attempt)) {
        this.sdkRetryCount = Math.max(this.sdkRetryCount ?? 0, properties.attempt)
      }
      return { events: [] }
    }
    if (type === "session.next.compaction.started" || type === "session.compacted") {
      this.contextCompacted = true
      return { events: [] }
    }
    return undefined
  }

  private v2ToolEvent(
    properties: Record<string, unknown>,
    status: "started" | "updated" | "finished",
    success?: boolean,
  ): StableEvent {
    const partID = typeof properties.callID === "string" ? properties.callID : "unknown"
    const tool = typeof properties.tool === "string" ? properties.tool : "unknown"
    const input = isRecord(properties.input) ? properties.input : {}
    const inputRaw = JSON.stringify(input)
    const result = properties.result ?? properties.content ?? properties.error ?? ""
    const outputRaw = typeof result === "string" ? result : JSON.stringify(result)
    const payload: Record<string, unknown> = {
      tool,
      part_id: partID,
      input_bytes: Buffer.byteLength(inputRaw),
      input_sha256: digest(inputRaw),
      status,
    }
    if (status === "finished") {
      payload.success = success === true
      payload.output_bytes = Buffer.byteLength(outputRaw)
      payload.output_sha256 = digest(outputRaw)
    }
    return { type: status === "started" ? "tool_started" : status === "updated" ? "tool_updated" : "tool_finished", payload }
  }

  private mapDelta(properties: Record<string, unknown>): StableEvent[] {
    const partID = typeof properties.partID === "string" ? properties.partID : ""
    const delta = typeof properties.delta === "string" ? properties.delta : ""
    const kind = this.partKinds.get(partID)
    if (!delta) return []
    if (kind === "reasoning") {
      return [{ type: "reasoning_delta", payload: { chars: delta.length, bytes: Buffer.byteLength(delta) } }]
    }
    if (kind === "text" || properties.field === "text") {
      return [{ type: "assistant_delta", payload: { text: delta } }]
    }
    return []
  }

  private mapPart(value: unknown): StableEvent[] {
    if (!isRecord(value) || typeof value.id !== "string" || typeof value.type !== "string") return []
    this.partKinds.set(value.id, value.type)
    if (value.type === "reasoning" && typeof value.text === "string") {
      return [
        {
          type: "reasoning_delta",
          payload: { chars: value.text.length, bytes: Buffer.byteLength(value.text), snapshot: true },
        },
      ]
    }
    if (value.type === "retry" && typeof value.attempt === "number" && Number.isInteger(value.attempt)) {
      this.sdkRetryCount = Math.max(this.sdkRetryCount ?? 0, value.attempt)
      return []
    }
    if (value.type === "tool" && typeof value.tool === "string" && isRecord(value.state)) {
      return this.mapTool(value.id, value.tool, value.state)
    }
    if (value.type === "step-finish" && isRecord(value.tokens)) {
      return [{ type: "token_usage", payload: normalizeUsage(value.tokens) }]
    }
    return []
  }

  private mapTool(partID: string, name: string, state: Record<string, unknown>): StableEvent[] {
    const status = typeof state.status === "string" ? state.status : "unknown"
    const previous = this.toolStates.get(partID)
    if (previous === status) return []
    this.toolStates.set(partID, status)
    const input = isRecord(state.input) ? state.input : {}
    const inputRaw = JSON.stringify(input)
    const common = {
      tool: name,
      part_id: partID,
      input_bytes: Buffer.byteLength(inputRaw),
      input_sha256: digest(inputRaw),
    }
    if (!previous && ["pending", "running"].includes(status)) {
      return [{ type: "tool_started", payload: { ...common, status } }]
    }
    if (status === "completed" || status === "error") {
      const output = typeof state.output === "string" ? state.output : typeof state.error === "string" ? state.error : ""
      return [
        {
          type: "tool_finished",
          payload: {
            ...common,
            status,
            success: status === "completed",
            output_bytes: Buffer.byteLength(output),
            output_sha256: digest(output),
            truncated: isRecord(state.metadata) ? Boolean(state.metadata.truncated) : false,
          },
        },
      ]
    }
    return [{ type: "tool_updated", payload: { ...common, status } }]
  }

  private mapMessage(value: unknown): StableEvent[] {
    if (!isRecord(value) || value.role !== "assistant" || !isRecord(value.tokens)) return []
    return [{ type: "token_usage", payload: normalizeUsage(value.tokens) }]
  }
}

export function normalizeUsage(tokens: Record<string, unknown>): Record<string, unknown> {
  const cache = isRecord(tokens.cache) ? tokens.cache : {}
  return {
    input: integerOrNull(tokens.input),
    output: integerOrNull(tokens.output),
    reasoning: integerOrNull(tokens.reasoning),
    cache_read: integerOrNull(cache.read),
    cache_write: integerOrNull(cache.write),
    total: integerOrNull(tokens.total),
    raw: tokens,
  }
}

function integerOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex")
}
