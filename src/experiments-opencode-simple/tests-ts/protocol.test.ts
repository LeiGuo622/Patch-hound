import assert from "node:assert/strict"
import test from "node:test"

import { outgoing, parseHostMessage } from "../sidecar/protocol.js"

function host(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    schema_version: 1,
    type: "initialize",
    id: "request-1",
    timestamp: new Date().toISOString(),
    payload: {},
    ...overrides,
  })
}

test("parseHostMessage accepts the strict schema", () => {
  const parsed = parseHostMessage(host(), 4096)
  assert.equal(parsed.id, "request-1")
  assert.equal(parsed.type, "initialize")
})

test("parseHostMessage rejects versions, unknown fields, and oversized data", () => {
  assert.throws(() => parseHostMessage(host({ schema_version: 2 }), 4096), /schema_version/)
  assert.throws(() => parseHostMessage(host({ extra: true }), 4096), /unknown message fields/)
  assert.throws(() => parseHostMessage(host(), 4), /exceeds/)
})

test("outgoing messages preserve request, session, and turn correlation", () => {
  const value = outgoing("turn_finished", {}, { requestID: "r", sessionID: "s", turnID: "t" })
  assert.equal(value.schema_version, 1)
  assert.equal(value.request_id, "r")
  assert.equal(value.session_id, "s")
  assert.equal(value.turn_id, "t")
})
