import { randomUUID } from "node:crypto"

export const SCHEMA_VERSION = 1

export const HOST_TYPES = new Set(["initialize", "start_session", "prompt", "abort", "dispose"])

export type HostMessage = {
  schema_version: number
  type: "initialize" | "start_session" | "prompt" | "abort" | "dispose"
  id: string
  timestamp: string
  payload: Record<string, unknown>
}

export type SidecarType =
  | "ready"
  | "session_started"
  | "mcp_status"
  | "turn_started"
  | "assistant_delta"
  | "reasoning_delta"
  | "tool_started"
  | "tool_updated"
  | "tool_finished"
  | "token_usage"
  | "permission_denied"
  | "turn_finished"
  | "error"
  | "disposed"

export type SidecarMessage = {
  schema_version: number
  type: SidecarType
  id: string
  timestamp: string
  payload: Record<string, unknown>
  request_id?: string
  session_id?: string
  turn_id?: string
}

export function parseHostMessage(line: string, maxBytes: number): HostMessage {
  if (Buffer.byteLength(line, "utf8") > maxBytes) throw new Error(`message exceeds ${maxBytes} bytes`)
  const raw: unknown = JSON.parse(line)
  if (!isRecord(raw)) throw new Error("message must be an object")
  const allowed = new Set(["schema_version", "type", "id", "timestamp", "payload"])
  const unknown = Object.keys(raw).filter((key) => !allowed.has(key))
  if (unknown.length) throw new Error(`unknown message fields: ${unknown.sort().join(",")}`)
  if (raw.schema_version !== SCHEMA_VERSION) throw new Error(`unsupported schema_version: ${String(raw.schema_version)}`)
  if (typeof raw.type !== "string" || !HOST_TYPES.has(raw.type)) throw new Error(`unknown host type: ${String(raw.type)}`)
  if (typeof raw.id !== "string" || !raw.id) throw new Error("id must be a non-empty string")
  if (typeof raw.timestamp !== "string" || !raw.timestamp) throw new Error("timestamp must be a non-empty string")
  if (!isRecord(raw.payload)) throw new Error("payload must be an object")
  return raw as HostMessage
}

export function outgoing(
  type: SidecarType,
  payload: Record<string, unknown>,
  context: { requestID?: string; sessionID?: string; turnID?: string } = {},
): SidecarMessage {
  return {
    schema_version: SCHEMA_VERSION,
    type,
    id: randomUUID().replaceAll("-", ""),
    timestamp: new Date().toISOString(),
    payload,
    ...(context.requestID ? { request_id: context.requestID } : {}),
    ...(context.sessionID ? { session_id: context.sessionID } : {}),
    ...(context.turnID ? { turn_id: context.turnID } : {}),
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function requireString(record: Record<string, unknown>, key: string): string {
  const value = record[key]
  if (typeof value !== "string" || !value) throw new Error(`${key} must be a non-empty string`)
  return value
}

export function optionalNumber(record: Record<string, unknown>, key: string): number | undefined {
  const value = record[key]
  if (value === undefined) return undefined
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${key} must be a finite number`)
  return value
}

