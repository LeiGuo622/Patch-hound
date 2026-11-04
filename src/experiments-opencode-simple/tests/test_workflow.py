from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_NO_ISSUES

from opencode_simple_agent.config import RuntimeConfig
from opencode_simple_agent.events import TurnExecution
from opencode_simple_agent.history_mcp import EXPOSED_TOOL_NAME, HistoryMcpDefinition
from opencode_simple_agent.isolation import IsolationProbeResult
from opencode_simple_agent.protocol import ProtocolMessage
from opencode_simple_agent.workflow import (
    AuthRequiredError,
    InvalidResponseError,
    _first_touched_path,
    run_case,
)


def protocol(kind: str, payload: dict[str, Any], *, session: str | None = None) -> ProtocolMessage:
    return ProtocolMessage(kind, f"id-{kind}", datetime.now(UTC).isoformat(), payload, session_id=session)


def isolation_ok(_source: Path, _layout: Any) -> IsolationProbeResult:
    return IsolationProbeResult(
        source_read=True,
        source_write_blocked=True,
        source_rename_blocked=True,
        source_delete_blocked=True,
        scratch_write=True,
        runtime_unreadable_from_agent_shell=True,
        repository_unreadable_from_agent_shell=True,
        tcp_network_blocked=True,
        udp_network_blocked=True,
        network_namespace_shared=True,
    )


class FakeRuntime:
    def __init__(self, responses: list[str], *, model: str = "gpt-5.6-sol", timeout: bool = False) -> None:
        self.responses = responses
        self.model = model
        self.timeout = timeout
        self.prompts: list[tuple[str, str]] = []
        self.started = False
        self.disposed = False
        self.aborted = False
        self.session_calls = 0

    def start(self) -> None:
        self.started = True

    def initialize(self, payload: dict[str, Any], *, timeout: float) -> ProtocolMessage:
        assert payload["model_selector"] == "openai/gpt-5.6-sol"
        assert timeout > 0
        return protocol(
            "ready",
            {
                "cliVersion": "1.18.25",
                "sdkVersion": "1.18.25",
                "serverVersion": "1.18.25",
                "configuredModel": "openai/gpt-5.6-sol",
                "configuredReasoning": "high",
                "permissionPolicy": {
                    "*": "deny",
                    "external_directory": "deny",
                    "bash": "allow",
                    "read": "allow",
                    "glob": "allow",
                    "grep": "allow",
                    "patchhound_history_query_pre_patch_history": "allow",
                    "apply_patch": "deny",
                    "edit": "deny",
                    "write": "deny",
                    "task": "deny",
                    "skill": "deny",
                    "lsp": "deny",
                    "todowrite": "deny",
                    "question": "deny",
                    "websearch": "deny",
                    "webfetch": "deny",
                },
            },
        )

    def start_session(self, _payload: dict[str, Any], *, timeout: float) -> tuple[ProtocolMessage, ProtocolMessage]:
        assert timeout > 0
        self.session_calls += 1
        config = RuntimeConfig.load()
        started = protocol("session_started", {"effective_tools": list(config.enabled_tools)}, session="session-1")
        mcp = protocol(
            "mcp_status",
            {
                "server": "patchhound_history",
                "status": "connected",
                "error": None,
                "tool": EXPOSED_TOOL_NAME,
                "schema_sha256": "schema",
                "call_probe": "succeeded",
            },
            session="session-1",
        )
        return started, mcp

    def prompt(self, name: str, text: str, *, timeout: float) -> TurnExecution:
        self.prompts.append((name, text))
        if self.timeout:
            raise TimeoutError("fake deadline")
        response = self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]
        return TurnExecution(
            name=name,
            turn_id=f"turn-{len(self.prompts)}",
            final_response=response,
            finish_reason="stop",
            provider="openai",
            model=self.model,
            reasoning_effort="high",
            usage={"input": 1, "output": 2, "reasoning": None, "cache_read": None, "cache_write": None, "total": 3},
            events=(),
            context_compacted=False,
            sdk_retry_count=None,
        )

    def abort(self, *, timeout: float) -> None:
        assert timeout > 0
        self.aborted = True

    def dispose(self) -> None:
        self.disposed = True


def definition() -> HistoryMcpDefinition:
    return HistoryMcpDefinition("patchhound_history", "query_pre_patch_history", {}, "schema")


def execute(
    prepared_cases: Path,
    tmp_path: Path,
    runtime: FakeRuntime,
    **kwargs: Any,
) -> Any:
    def factory(_command: list[str], _config: RuntimeConfig) -> FakeRuntime:
        return runtime

    return run_case(
        "case_0001",
        cases_dir=prepared_cases,
        output_root=tmp_path / "runs",
        phoenix_enabled=False,
        runtime_factory=factory,
        isolation_probe_factory=isolation_ok,
        history_definition=definition(),
        live_output=False,
        **kwargs,
    )


def manifest(output_root: Path) -> dict[str, Any]:
    return json.loads(next(output_root.glob("run_*/run_manifest.json")).read_text(encoding="utf-8"))


def test_success_writes_comparable_artifacts(prepared_cases: Path, tmp_path: Path) -> None:
    runtime = FakeRuntime([VALID_NO_ISSUES])
    result = execute(prepared_cases, tmp_path, runtime, save_conversation=True)
    data = json.loads((result.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert result.assessment == "NO_NEW_ISSUES"
    assert result.output_path.read_text(encoding="utf-8").startswith("# Patch Security Analysis")
    assert data["status"] == "completed"
    assert data["formal_comparison"] is True
    assert data["effective_model"] == "gpt-5.6-sol"
    assert data["isolation_probe"]["source_write_blocked"] is True
    assert runtime.session_calls == 1 and runtime.disposed
    assert (result.run_dir / "conversation.md").is_file()
    model_config = (result.run_dir / "model_config.md").read_text(encoding="utf-8")
    assert "- Agent tool network: `disabled`" in model_config
    assert "- Control-plane network: `enabled`" in model_config
    assert (result.run_dir / "history_queries.md").read_text(encoding="utf-8").startswith(
        "# Pre-Patch History Queries"
    )
    process_log = (result.run_dir / "process_log.md").read_text(encoding="utf-8")
    for component, action in (
        ("runtime", "start"),
        ("runtime", "initialize"),
        ("runtime", "session.create"),
        ("mcp", "initialize"),
        ("runtime", "complete"),
        ("runtime", "dispose"),
    ):
        assert f"| {component} | {action} |" in process_log


def test_contract_repair_stays_in_same_session(prepared_cases: Path, tmp_path: Path) -> None:
    previous = "invalid previous response"
    runtime = FakeRuntime([previous, VALID_NO_ISSUES])
    result = execute(prepared_cases, tmp_path, runtime)
    assert result.turn_ids == ("turn-1", "turn-2")
    assert runtime.session_calls == 1
    assert [name for name, _text in runtime.prompts] == ["initial", "contract_repair_1"]
    assert "INVALID_TITLE" in runtime.prompts[1][1]
    assert previous not in runtime.prompts[1][1]


def test_final_invalid_response_has_distinct_status(prepared_cases: Path, tmp_path: Path) -> None:
    runtime = FakeRuntime(["invalid"])
    with pytest.raises(InvalidResponseError):
        execute(prepared_cases, tmp_path, runtime)
    data = manifest(tmp_path / "runs")
    assert data["status"] == "invalid_response"
    assert data["error_class"] == "RESPONSE_CONTRACT"
    assert next((tmp_path / "runs").glob("run_*/invalid_response.md")).is_file()
    assert len(runtime.prompts) == 3


def test_model_fallback_is_rejected(prepared_cases: Path, tmp_path: Path) -> None:
    runtime = FakeRuntime([VALID_NO_ISSUES], model="different-model")
    with pytest.raises(Exception, match="effective model mismatch"):
        execute(prepared_cases, tmp_path, runtime)
    data = manifest(tmp_path / "runs")
    assert data["status"] == "failed"
    assert data["effective_model"] == "different-model"
    assert data["model_fallback"] is True


def test_timeout_aborts_before_dispose(prepared_cases: Path, tmp_path: Path) -> None:
    runtime = FakeRuntime([VALID_NO_ISSUES], timeout=True)
    with pytest.raises(TimeoutError):
        execute(prepared_cases, tmp_path, runtime)
    assert runtime.aborted and runtime.disposed
    assert manifest(tmp_path / "runs")["status"] == "timeout"


def test_missing_auth_is_diagnostic_before_isolation(prepared_cases: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PATCHHOUND_OPENCODE_DATA_DIR", raising=False)
    called = False

    def forbidden_probe(_source: Path, _layout: Any) -> IsolationProbeResult:
        nonlocal called
        called = True
        raise AssertionError("isolation must not start before auth preflight")

    with pytest.raises(AuthRequiredError, match="AUTH_REQUIRED"):
        run_case(
            "case_0001",
            cases_dir=prepared_cases,
            output_root=tmp_path / "runs",
            phoenix_enabled=False,
            isolation_probe_factory=forbidden_probe,
            live_output=False,
        )
    assert not called
    assert manifest(tmp_path / "runs")["status"] == "auth_required"


@pytest.mark.parametrize("diff", [
    "diff --git a/../secret b/../secret\n",
    "diff --git a/src/x b/src/../x\n",
    "+++ b//absolute-ish\n",
    "+++ b/C:\\secret\n",
    "+++ b//dev/null\n",
])
def test_history_touched_path_rejects_unsafe_values(diff: str) -> None:
    with pytest.raises(Exception, match="unsafe touched path"):
        _first_touched_path(diff)


def test_first_touched_path_accepts_prepared_case_format() -> None:
    diff = "File: drivers/net/example.c\nBefore: drivers/net/example.c\nAfter: drivers/net/example.c\n@@ -1 +1 @@\n"
    assert _first_touched_path(diff) == "drivers/net/example.c"


def test_first_touched_path_rejects_prepared_case_traversal() -> None:
    with pytest.raises(Exception, match="unsafe touched path"):
        _first_touched_path("File: ../../agent.db\n")
