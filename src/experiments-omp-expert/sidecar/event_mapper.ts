import { createHash } from "node:crypto"

export type StableEvent = {
  type: "assistant_delta" | "reasoning_delta" | "tool_started" | "tool_updated" | "tool_finished" | "token_usage" | "permission_denied"
  payload: Record<string, unknown>
}

export class EventMapper {
  contextCompacted = false
  sdkRetryCount: number | undefined

  map(event: unknown): StableEvent[] {
    if (!isRecord(event) || typeof event.type !== "string") return []
    switch (event.type) {
      case "message_update": {
        const update = isRecord(event.assistantMessageEvent) ? event.assistantMessageEvent : {}
        const kind = update.type
        if (kind === "text_delta" && typeof update.delta === "string") {
          return [{ type: "assistant_delta", payload: { text: update.delta } }]
        }
        if (kind === "thinking_delta" && typeof update.delta === "string") {
          return [{ type: "reasoning_delta", payload: { chars: update.delta.length, bytes: Buffer.byteLength(update.delta) } }]
        }
        if (kind === "done" && isRecord(update.message)) {
          return [{ type: "token_usage", payload: normalizeUsage(update.message.usage) }]
        }
        return []
      }
      case "tool_execution_start":
        return [this.toolEvent(event, "started")]
      case "tool_execution_update":
        return [this.toolEvent(event, "updated")]
      case "tool_execution_end":
        return [this.toolEvent(event, "finished", event.isError !== true)]
      case "auto_compaction_start":
      case "auto_compaction_end":
        this.contextCompacted = true
        return []
      case "auto_retry_start":
        if (typeof event.attempt === "number" && Number.isInteger(event.attempt)) {
          this.sdkRetryCount = Math.max(this.sdkRetryCount ?? 0, event.attempt)
        }
        return []
      case "retry_fallback_applied":
      case "retry_fallback_succeeded":
        return []
      case "permission_denied":
        return [{ type: "permission_denied", payload: { message: String(event.message ?? "permission denied") } }]
      default:
        return []
    }
  }

  private toolEvent(event: Record<string, unknown>, status: "started" | "updated" | "finished", success?: boolean): StableEvent {
    const toolCallId = typeof event.toolCallId === "string" ? event.toolCallId : "unknown"
    const toolName = typeof event.toolName === "string" ? event.toolName : "unknown"
    const args = isRecord(event.args) ? event.args : {}
    const argsRaw = JSON.stringify(args)
    const payload: Record<string, unknown> = {
      tool: toolName,
      part_id: toolCallId,
      input_bytes: Buffer.byteLength(argsRaw),
      input_sha256: digest(argsRaw),
      status,
    }
    if (status === "finished") {
      const result = event.result
      const outputRaw = typeof result === "string" ? result : JSON.stringify(result ?? "")
      payload.success = success === true
      payload.output_bytes = Buffer.byteLength(outputRaw)
      payload.output_sha256 = digest(outputRaw)
    }
    return { type: status === "started" ? "tool_started" : status === "updated" ? "tool_updated" : "tool_finished", payload }
  }
}

export function normalizeUsage(value: unknown): Record<string, unknown> {
  const usage = isRecord(value) ? value : {}
  const cache = isRecord(usage.cache) ? usage.cache : {}
  return {
    input: integerOrNull(usage.input),
    output: integerOrNull(usage.output),
    reasoning: integerOrNull(usage.reasoning),
    cache_read: integerOrNull(cache.read),
    cache_write: integerOrNull(cache.write),
    total: integerOrNull(usage.total),
    raw: usage,
  }
}

function integerOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex")
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}
