from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import VALID_NO_ISSUES

from omp_agent.config import RuntimeConfig
from omp_agent.events import TurnExecution
from omp_agent.history_mcp import EXPOSED_TOOL_NAME, HistoryMcpDefinition
from omp_agent.isolation import IsolationProbeResult
from omp_agent.protocol import ProtocolMessage
from omp_agent.workflow import AuthRequiredError, InvalidResponseError, _first_touched_path, run_case


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
    def __init__(
        self,
        responses: list[str],
        *,
        model: str = "gpt-5.6-sol",
        timeout: bool = False,
        ready_overrides: dict[str, Any] | None = None,
        effective_tools: list[str] | None = None,
        mcp_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.responses = responses
        self.model = model
        self.timeout = timeout
        self.prompts: list[tuple[str, str]] = []
        self.started = False
        self.disposed = False
        self.aborted = False
        self.session_calls = 0
        self.ready_overrides = ready_overrides or {}
        self.effective_tools = effective_tools
        self.mcp_overrides = mcp_overrides or {}

    def start(self) -> None:
        self.started = True

    def initialize(self, payload: dict[str, Any], *, timeout: float) -> ProtocolMessage:
        assert payload["model_selector"] == "openai-codex/gpt-5.6-sol"
        assert timeout > 0
        config = RuntimeConfig.load()
        payload = {
            "sdkVersion": "18.0.11",
            "bunVersion": "1.4.0",
            "configuredModel": config.model_selector,
            "configuredReasoning": config.reasoning_effort,
            "permissionPolicy": {
                "autoApprove": True,
                "restrictedTools": True,
                "disabledTools": list(config.disabled_tools),
                "extensions": "disabled",
                "mcp": "bridge-only",
                "lsp": "disabled",
                "shellPath": "/app/bin/bash",
            },
        }
        payload.update(self.ready_overrides)
        return protocol("ready", payload)

    def start_session(self, _payload: dict[str, Any], *, timeout: float) -> tuple[ProtocolMessage, ProtocolMessage]:
        assert timeout > 0
        self.session_calls += 1
        config = RuntimeConfig.load()
        started = protocol(
            "session_started",
            {"effective_tools": self.effective_tools or list(config.enabled_tools)},
            session="session-1",
        )
        mcp_payload = {
            "server": "patchhound_history",
            "status": "connected",
            "error": None,
            "tool": EXPOSED_TOOL_NAME,
            "schema_sha256": "schema",
            "call_probe": "succeeded",
        }
        mcp_payload.update(self.mcp_overrides)
        mcp = protocol("mcp_status", mcp_payload, session="session-1")
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
            provider="openai-codex",
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

    # These fake-runtime tests need mountable paths, not a real Linux checkout.
    history_repo = tmp_path / "history-repo"
    history_metadata = tmp_path / "history-metadata"
    history_repo.mkdir(exist_ok=True)
    history_metadata.mkdir(exist_ok=True)

    return run_case(
        "case_0001",
        cases_dir=prepared_cases,
        output_root=tmp_path / "runs",
        phoenix_enabled=False,
        runtime_factory=factory,
        isolation_probe_factory=isolation_ok,
        history_definition=definition(),
        history_linux_repo=history_repo,
        history_metadata_dir=history_metadata,
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


def test_extra_effective_tool_is_rejected(prepared_cases: Path, tmp_path: Path) -> None:
    config = RuntimeConfig.load()
    runtime = FakeRuntime([VALID_NO_ISSUES], effective_tools=[*config.enabled_tools, "todo"])
    with pytest.raises(Exception, match="effective tool set mismatch"):
        execute(prepared_cases, tmp_path, runtime)
    assert manifest(tmp_path / "runs")["status"] == "failed"


def test_ready_configuration_mismatch_is_rejected(prepared_cases: Path, tmp_path: Path) -> None:
    runtime = FakeRuntime([VALID_NO_ISSUES], ready_overrides={"configuredReasoning": "medium"})
    with pytest.raises(Exception, match="configured reasoning mismatch"):
        execute(prepared_cases, tmp_path, runtime)
    assert manifest(tmp_path / "runs")["status"] == "failed"


def test_history_schema_mismatch_is_rejected(prepared_cases: Path, tmp_path: Path) -> None:
    runtime = FakeRuntime([VALID_NO_ISSUES], mcp_overrides={"schema_sha256": "wrong"})
    with pytest.raises(Exception, match="history MCP status mismatch"):
        execute(prepared_cases, tmp_path, runtime)
    assert manifest(tmp_path / "runs")["status"] == "failed"


def test_first_touched_path_accepts_prepared_case_format() -> None:
    diff = "File: drivers/net/example.c\nBefore: drivers/net/example.c\nAfter: drivers/net/example.c\n@@ -1 +1 @@\n"
    assert _first_touched_path(diff) == "drivers/net/example.c"


def test_first_touched_path_rejects_traversal() -> None:
    with pytest.raises(Exception, match="unsafe touched path"):
        _first_touched_path("File: ../../agent.db\n")


def test_timeout_aborts_before_dispose(prepared_cases: Path, tmp_path: Path) -> None:
    runtime = FakeRuntime([VALID_NO_ISSUES], timeout=True)
    with pytest.raises(TimeoutError):
        execute(prepared_cases, tmp_path, runtime)
    assert runtime.aborted and runtime.disposed
    assert manifest(tmp_path / "runs")["status"] == "timeout"


def test_missing_auth_is_diagnostic_before_isolation(prepared_cases: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PATCHHOUND_OMP_DATA_DIR", raising=False)
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


def test_auth_directory_without_agent_database_is_diagnostic(
    prepared_cases: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = tmp_path / "omp-agent"
    auth.mkdir()
    monkeypatch.setenv("PATCHHOUND_OMP_DATA_DIR", str(auth))
    with pytest.raises(AuthRequiredError, match="agent database"):
        run_case(
            "case_0001",
            cases_dir=prepared_cases,
            output_root=tmp_path / "runs",
            phoenix_enabled=False,
            isolation_probe_factory=isolation_ok,
            live_output=False,
        )
    assert manifest(tmp_path / "runs")["status"] == "auth_required"
