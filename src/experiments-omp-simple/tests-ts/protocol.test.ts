import { expect, test } from "bun:test"

import { outgoing, parseHostMessage } from "../sidecar/protocol.js"

test("sidecar protocol rejects unknown fields and preserves request context", () => {
  const message = outgoing("ready", { sdkVersion: "18.0.11" }, { requestID: "req-1" })
  expect(message.request_id).toBe("req-1")
  expect(message.schema_version).toBe(1)
  expect(() => parseHostMessage(JSON.stringify({ ...message, type: "initialize", extra: true }), 4096)).toThrow()
})

test("sidecar protocol accepts a valid initialize message", () => {
  const parsed = parseHostMessage(
    JSON.stringify({
      schema_version: 1,
      type: "initialize",
      id: "init-1",
      timestamp: new Date().toISOString(),
      payload: { omp_sdk_version: "18.0.11" },
    }),
    4096,
  )
  expect(parsed.payload.omp_sdk_version).toBe("18.0.11")
})
