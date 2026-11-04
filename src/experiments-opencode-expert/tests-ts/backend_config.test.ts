import assert from "node:assert/strict"
import test from "node:test"

import { buildConfig, describeSdkError, permissionPolicy, sha256Canonical, toolSwitches } from "../sidecar/opencode_backend.js"

const base = {
  model_selector: "openai/gpt-5.6-sol",
  reasoning_effort: "high",
  history_enabled: true,
  mcp_command: ["/usr/bin/node", "/app/dist/sidecar/mcp_proxy.js", "/runtime/history-mcp.sock"],
  enabled_tools: ["bash", "read", "patchhound_history_query_pre_patch_history"],
  disabled_tools: ["write", "edit", "task"],
}

test("explicit SDK config disables ambient capabilities", () => {
  const permission = permissionPolicy(base)
  const config = buildConfig(base, permission)
  assert.equal(config.model, "openai/gpt-5.6-sol")
  assert.equal(config.provider?.openai?.options?.timeout, 3_600_000)
  assert.equal(config.provider?.openai?.options?.headerTimeout, 3_600_000)
  assert.equal(config.default_agent, "build")
  assert.deepEqual(config.plugin, [])
  assert.deepEqual(config.instructions, [])
  assert.deepEqual(config.skills, { paths: [], urls: [] })
  assert.equal(config.subagent_depth, 0)
  assert.equal(config.agent?.plan && "disable" in config.agent.plan ? config.agent.plan.disable : undefined, true)
  assert.equal((permission as Record<string, unknown>)["*"], "deny")
  assert.equal((permission as Record<string, unknown>).write, "deny")
  assert.deepEqual(toolSwitches(base), {
    bash: true,
    read: true,
    patchhound_history_query_pre_patch_history: true,
    write: false,
    edit: false,
    task: false,
  })
})

test("provider request timeout follows the host deadline", () => {
  const payload = { ...base, provider_timeout_ms: 123_456 }
  const config = buildConfig(payload, permissionPolicy(payload))
  assert.equal(config.provider?.openai?.options?.timeout, 123_456)
  assert.equal(config.provider?.openai?.options?.headerTimeout, 123_456)
})

test("Coding Plan uses the Chat Completions compatible provider", () => {
  const payload = { ...base, model_selector: "zhipu-coding-plan/glm-5.3" }
  const config = buildConfig(payload, permissionPolicy(payload))
  const provider = config.provider?.["zhipu-coding-plan"]
  assert.equal(provider?.npm, "@ai-sdk/openai-compatible")
  assert.equal(provider?.options?.baseURL, "https://open.bigmodel.cn/api/coding/paas/v4")
  assert.equal(provider?.options?.apiKey, "{env:PATCHHOUND_GLM_CODING_API_KEY}")
  assert.equal(provider?.options?.timeout, 3_600_000)
  assert.deepEqual(provider?.models?.["glm-5.3"]?.limit, { context: 256_000, output: 32_768 })
})

test("NewAPI DeepSeek uses the comparison endpoint, key, and model", () => {
  const payload = {
    ...base,
    model_selector: "newapi/deepseek/deepseek-v4-flash",
    provider_timeout_ms: 123_456,
  }
  const config = buildConfig(payload, permissionPolicy(payload))
  const provider = config.provider?.newapi
  assert.equal(config.model, "newapi/deepseek/deepseek-v4-flash")
  assert.equal(provider?.npm, "@ai-sdk/openai")
  assert.equal(provider?.options?.baseURL, "https://api.example.invalid/v1")
  assert.equal(provider?.options?.apiKey, "{env:PATCHHOUND_DEEPSEEK_API_KEY}")
  assert.equal(provider?.options?.timeout, 123_456)
  assert.ok(provider?.models?.["deepseek/deepseek-v4-flash"])
})

test("SDK transport errors retain their diagnostic message", () => {
  const error = new Error("fetch failed", { cause: { code: "ECONNRESET" } })
  assert.equal(describeSdkError(error), "Error: fetch failed (code ECONNRESET)")
  assert.equal(describeSdkError({ message: "server rejected request" }), "server rejected request")
})

test("history-disabled config starts no MCP process", () => {
  const payload = { ...base, history_enabled: false, mcp_command: [] }
  const config = buildConfig(payload, permissionPolicy(payload))
  assert.deepEqual(config.mcp, {})
})

test("canonical schema hashing matches Python sort_keys semantics", () => {
  assert.equal(
    sha256Canonical({ b: 1, a: { d: 2, c: 3 } }),
    "78d48859c3252943aab7306f76c80f3f07783582e05ab8f944ce0696f2dbfc67",
  )
})
