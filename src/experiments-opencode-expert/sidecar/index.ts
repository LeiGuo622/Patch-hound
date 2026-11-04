import { randomUUID } from "node:crypto"
import { createInterface } from "node:readline"

import { OpenCodeBackend } from "./opencode_backend.js"
import { outgoing, parseHostMessage, requireString, type SidecarMessage, type SidecarType } from "./protocol.js"

let maxMessageBytes = 1024 * 1024
let initialized = false
let sessionID: string | undefined
let disposed = false
let activePrompt: Promise<void> | undefined
let abortRequested = false
const seenRequestIDs = new Set<string>()

function write(message: SidecarMessage): void {
  process.stdout.write(`${JSON.stringify(message)}\n`)
}

function emit(
  type: SidecarType,
  payload: Record<string, unknown>,
  context: { requestID?: string; sessionID?: string; turnID?: string } = {},
): void {
  write(outgoing(type, payload, context))
}

const backend = new OpenCodeBackend((type, payload, context) => emit(type, payload, context))
const reader = createInterface({ input: process.stdin, crlfDelay: Infinity })

for await (const line of reader) {
  if (!line.trim()) continue
  let requestID: string | undefined
  try {
    const message = parseHostMessage(line, maxMessageBytes)
    requestID = message.id
    if (seenRequestIDs.has(requestID)) throw new Error(`duplicate request id: ${requestID}`)
    seenRequestIDs.add(requestID)
    if (disposed) throw new Error("sidecar is already disposed")
    if (message.type === "initialize") {
      if (initialized) throw new Error("initialize may only be sent once")
      const configuredLimit = message.payload.message_bytes
      if (typeof configuredLimit === "number" && configuredLimit > 0) maxMessageBytes = configuredLimit
      const result = await backend.initialize(message.payload)
      initialized = true
      emit("ready", result, { requestID })
      continue
    }
    if (!initialized) throw new Error("initialize must be the first request")
    if (message.type === "start_session") {
      if (sessionID) throw new Error("only one session may be created")
      const result = await backend.startSession()
      sessionID = result.sessionID
      emit(
        "session_started",
        { effective_tools: result.effectiveTools, restored: false },
        { requestID, sessionID },
      )
      emit("mcp_status", result.mcp, { requestID, sessionID })
      continue
    }
    if (message.type === "prompt") {
      if (!sessionID) throw new Error("start_session must precede prompt")
      if (activePrompt) throw new Error("a prompt is already active")
      const name = requireString(message.payload, "name")
      const text = requireString(message.payload, "text")
      const turnID = randomUUID().replaceAll("-", "")
      const promptRequestID = message.id
      abortRequested = false
      emit("turn_started", { name }, { requestID: promptRequestID, sessionID, turnID })
      const pending = backend
        .prompt(promptRequestID, name, text, turnID)
        .then((result) => {
          if (abortRequested) return
          emit("token_usage", result.usage, { requestID: promptRequestID, sessionID: sessionID!, turnID })
          emit(
            "turn_finished",
            {
              name,
              final_response: result.finalResponse,
              finish_reason: result.finishReason,
              provider: result.provider,
              model: result.model,
              reasoning_effort: result.reasoningEffort,
              context_compacted: result.contextCompacted,
              sdk_retry_count: result.sdkRetryCount,
            },
            { requestID: promptRequestID, sessionID: sessionID!, turnID },
          )
        })
        .catch((error: unknown) => {
          if (abortRequested) return
          const details = error instanceof Error ? error.message : String(error)
          process.stderr.write(`${details}\n`)
          emit(
            "error",
            { code: classify(details), message: details },
            { requestID: promptRequestID, sessionID: sessionID!, turnID },
          )
        })
        .finally(() => {
          if (activePrompt === pending) activePrompt = undefined
        })
      activePrompt = pending
      continue
    }
    if (message.type === "abort") {
      abortRequested = true
      await backend.abort()
      emit("error", { code: "ABORTED", message: "active turn aborted" }, { requestID, ...(sessionID ? { sessionID } : {}) })
      continue
    }
    if (message.type === "dispose") {
      if (activePrompt) {
        abortRequested = true
        await backend.abort().catch(() => undefined)
        await Promise.race([activePrompt, new Promise<void>((resolve) => setTimeout(resolve, 2_000))])
      }
      await backend.dispose()
      disposed = true
      emit("disposed", {}, { requestID, ...(sessionID ? { sessionID } : {}) })
      reader.close()
      break
    }
    throw new Error(`unsupported request type: ${message.type}`)
  } catch (error) {
    const details = error instanceof Error ? error.message : String(error)
    process.stderr.write(`${details}\n`)
    emit(
      "error",
      { code: classify(details), message: details },
      { ...(requestID ? { requestID } : {}), ...(sessionID ? { sessionID } : {}) },
    )
  }
}

if (!disposed) await backend.dispose().catch(() => undefined)

function classify(message: string): string {
  if (/auth|credential|login/i.test(message)) return "AUTH_REQUIRED"
  if (/model/i.test(message)) return "MODEL_MISMATCH"
  if (/mcp/i.test(message)) return "MCP_ERROR"
  if (/version/i.test(message)) return "VERSION_MISMATCH"
  return "OPENCODE_ERROR"
}
