import assert from "node:assert/strict"
import test from "node:test"

import { EventMapper, normalizeUsage } from "../sidecar/event_mapper.js"

test("reasoning deltas expose counts but never text", () => {
  const mapper = new EventMapper()
  mapper.map({ type: "message.part.updated", properties: { part: { id: "p1", type: "reasoning" } } })
  const result = mapper.map({
    type: "message.part.delta",
    properties: { partID: "p1", field: "text", delta: "hidden reasoning" },
  })
  assert.equal(result.events[0]?.type, "reasoning_delta")
  assert.deepEqual(result.events[0]?.payload, { chars: 16, bytes: 16 })
  assert.equal("text" in (result.events[0]?.payload ?? {}), false)
})

test("tool events are stable, hashed, and de-duplicated", () => {
  const mapper = new EventMapper()
  const pending = {
    type: "message.part.updated",
    properties: { part: { id: "tool-1", type: "tool", tool: "read", state: { status: "pending", input: { file: "x" } } } },
  }
  assert.equal(mapper.map(pending).events[0]?.type, "tool_started")
  assert.equal(mapper.map(pending).events.length, 0)
  const completed = mapper.map({
    type: "message.part.updated",
    properties: {
      part: {
        id: "tool-1",
        type: "tool",
        tool: "read",
        state: { status: "completed", input: { file: "x" }, output: "contents" },
      },
    },
  })
  assert.equal(completed.events[0]?.type, "tool_finished")
  assert.equal(typeof completed.events[0]?.payload.output_sha256, "string")
  assert.equal("output" in (completed.events[0]?.payload ?? {}), false)
})

test("token usage keeps missing values null", () => {
  assert.deepEqual(normalizeUsage({ input: 2, output: 3, cache: { read: 1 } }), {
    input: 2,
    output: 3,
    reasoning: null,
    cache_read: 1,
    cache_write: null,
    total: null,
    raw: { input: 2, output: 3, cache: { read: 1 } },
  })
})

test("native v2 text, tool, retry, and compaction events are normalized", () => {
  const mapper = new EventMapper()
  const text = mapper.map({
    type: "session.next.text.delta",
    properties: { textID: "text-1", delta: "answer" },
  })
  assert.deepEqual(text.events[0], { type: "assistant_delta", payload: { text: "answer" } })
  const tool = mapper.map({
    type: "session.next.tool.called",
    properties: { callID: "call-1", tool: "bash", input: { command: "pwd" } },
  })
  assert.equal(tool.events[0]?.type, "tool_started")
  const complete = mapper.map({
    type: "session.next.tool.success",
    properties: { callID: "call-1", tool: "bash", result: "ok" },
  })
  assert.equal(complete.events[0]?.type, "tool_finished")
  mapper.map({ type: "session.next.retried", properties: { attempt: 2 } })
  mapper.map({ type: "session.next.compaction.started", properties: {} })
  assert.equal(mapper.sdkRetryCount, 2)
  assert.equal(mapper.contextCompacted, true)
})
