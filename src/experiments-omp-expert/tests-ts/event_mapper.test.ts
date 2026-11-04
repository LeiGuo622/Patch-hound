import { describe, expect, test } from "bun:test"

import { EventMapper, normalizeUsage } from "../sidecar/event_mapper.js"

describe("OMP event mapper", () => {
  test("maps streamed text and strips reasoning text from stable events", () => {
    const mapper = new EventMapper()
    expect(
      mapper.map({
        type: "message_update",
        assistantMessageEvent: { type: "text_delta", delta: "hello" },
      }),
    ).toEqual([{ type: "assistant_delta", payload: { text: "hello" } }])
    expect(
      mapper.map({
        type: "message_update",
        assistantMessageEvent: { type: "thinking_delta", delta: "reason" },
      }),
    ).toEqual([{ type: "reasoning_delta", payload: { chars: 6, bytes: 6 } }])
  })

  test("normalizes OMP usage without inventing missing counters", () => {
    expect(normalizeUsage({ input: 3, output: 4, cache: { read: 2 } })).toEqual({
      input: 3,
      output: 4,
      reasoning: null,
      cache_read: 2,
      cache_write: null,
      total: null,
      raw: { input: 3, output: 4, cache: { read: 2 } },
    })
  })
})
