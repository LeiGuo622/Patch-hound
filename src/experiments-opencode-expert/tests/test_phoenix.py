from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from opencode_expert_agent.config import ConfigurationError, PhoenixConfig
from opencode_expert_agent.events import TurnExecution
from opencode_expert_agent.phoenix import PhoenixObserver, normalize_collector_endpoint
from opencode_expert_agent.validation import ValidationResult


class RecordingExporter(SpanExporter):
    def __init__(self, *, fail: bool = False, secret: str = "") -> None:
        self.fail = fail
        self.secret = secret
        self.exports = 0
        self.closed = False
        self.spans: list[Any] = []

    def export(self, spans: Any) -> SpanExportResult:
        self.exports += 1
        self.spans.extend(spans)
        if self.fail:
            raise RuntimeError(f"collector rejected {self.secret}")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.closed = True


def test_endpoint_normalization_and_rejection() -> None:
    assert normalize_collector_endpoint("http://localhost:6006") == "http://localhost:6006/v1/traces"
    assert normalize_collector_endpoint("https://example.test/custom/") == "https://example.test/custom"
    with pytest.raises(ConfigurationError):
        normalize_collector_endpoint("file:///tmp/traces")


def test_export_failure_is_fail_open_and_redacted() -> None:
    warnings: list[str] = []
    secret = "phoenix-secret"
    exporter = RecordingExporter(fail=True, secret=secret)
    observer = PhoenixObserver.create(
        config=PhoenixConfig(enabled=True, api_key=secret),
        case_id="case_0001",
        run_id="run_0001",
        model="gpt-5.6-sol",
        prompt_sha256="hash",
        input_hashes={"diff_sha256": "diff"},
        warn=warnings.append,
        exporter_factory=lambda _endpoint, headers: exporter,
    )
    assert observer is not None
    observer.checkpoint("turn.initial", output_value={"ok": True})
    assert not observer.available
    assert warnings and secret not in warnings[-1] and "[REDACTED]" in warnings[-1]
    observer.complete(status="completed")


def test_turn_error_records_redacted_status_without_secondary_failure() -> None:
    warnings: list[str] = []
    observer = PhoenixObserver.create(
        config=PhoenixConfig(enabled=True),
        case_id="case_0001",
        run_id="run_0001",
        model="gpt-5.6-sol",
        prompt_sha256="hash",
        input_hashes={},
        warn=warnings.append,
        exporter_factory=lambda _endpoint, _headers: RecordingExporter(),
    )
    assert observer is not None
    handle = observer.turn_start(name="initial", prompt="test")
    assert handle is not None
    observer.turn_error(handle, RuntimeError("turn failed"))
    assert observer.available
    assert warnings == []
    observer.complete(status="failed", error=RuntimeError("turn failed"))


def test_root_span_exposes_aggregate_token_usage() -> None:
    exporter = RecordingExporter()
    observer = PhoenixObserver.create(
        config=PhoenixConfig(enabled=True),
        case_id="case_0001",
        run_id="run_0001",
        model="gpt-5.6-sol",
        prompt_sha256="hash",
        input_hashes={},
        warn=lambda _message: None,
        exporter_factory=lambda _endpoint, _headers: exporter,
    )
    assert observer is not None
    observer.record_turn(
        TurnExecution(
            name="initial",
            turn_id="turn-1",
            final_response="# report",
            finish_reason="stop",
            provider="openai",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            usage={"input": 10, "output": 4, "reasoning": 2, "cache_read": 8, "cache_write": 0, "total": 24},
            events=(),
            context_compacted=False,
            sdk_retry_count=None,
        ),
        ValidationResult(True, "NO_NEW_ISSUES", "HIGH", ()),
        prompt="test",
    )
    observer.complete(status="completed")
    root = next(span for span in exporter.spans if span.name == "patchhound.experiments_opencode_expert.run")
    assert root.attributes["llm.token_count.prompt"] == 10
    assert root.attributes["llm.token_count.completion"] == 4
    assert root.attributes["llm.token_count.total"] == 24


def test_disabled_phoenix_creates_no_exporter() -> None:
    called = False

    def factory(_endpoint: str, _headers: dict[str, str]) -> SpanExporter:
        nonlocal called
        called = True
        return RecordingExporter()

    observer = PhoenixObserver.create(
        config=PhoenixConfig(enabled=False),
        case_id="case_0001",
        run_id="run_0001",
        model="gpt-5.6-sol",
        prompt_sha256="hash",
        input_hashes={},
        warn=lambda _message: None,
        exporter_factory=factory,
    )
    assert observer is None
    assert not called
