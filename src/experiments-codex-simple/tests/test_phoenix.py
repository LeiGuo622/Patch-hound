from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from opentelemetry.sdk.trace.export import SpanExportResult

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from simple_agent.config import PhoenixConfig  # noqa: E402
from simple_agent.events import EventSummary  # noqa: E402
from simple_agent.phoenix import (  # noqa: E402
    PhoenixObserver,
    _CircuitBreakingExporter,
    normalize_collector_endpoint,
)
from simple_agent.validation import validate_final_response  # noqa: E402


class FakeSpan:
    def __init__(self, name: str, attributes: dict[str, object]) -> None:
        self.name = name
        self.attributes = dict(attributes)
        self.status = None
        self.ended = False
        self.end_time = None

    def get_span_context(self) -> SimpleNamespace:
        return SimpleNamespace(trace_id=1)

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def set_status(self, status: object) -> None:
        self.status = status

    def record_exception(self, _exc: BaseException) -> None:
        pass

    def end(self, *, end_time: int | None = None) -> None:
        self.ended = True
        self.end_time = end_time


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_span(
        self,
        name: str,
        *,
        context: object = None,
        attributes: dict[str, object],
        start_time: int | None = None,
    ) -> FakeSpan:
        del context
        span = FakeSpan(name, attributes)
        span.start_time = start_time
        self.spans.append(span)
        return span


class FakeProvider:
    def __init__(self) -> None:
        self.flushed = False
        self.closed = False

    def force_flush(self, timeout_millis: int) -> bool:
        self.flushed = timeout_millis == 10_000
        return True

    def shutdown(self) -> None:
        self.closed = True


class PhoenixTest(unittest.TestCase):
    def test_endpoint_normalization(self) -> None:
        self.assertEqual(
            normalize_collector_endpoint("http://127.0.0.1:6006"),
            "http://127.0.0.1:6006/v1/traces",
        )
        self.assertEqual(
            normalize_collector_endpoint("https://phoenix.example/v1/traces"),
            "https://phoenix.example/v1/traces",
        )

    def test_default_is_enabled_and_project_is_model_specific(self) -> None:
        config = PhoenixConfig.load(environ={})
        self.assertTrue(config.enabled)
        self.assertEqual(
            config.effective_project_name("gpt-5.6-sol"),
            "patchhound-experiments-codex-simple-gpt-5-6-sol",
        )

    def test_environment_and_cli_overrides_have_expected_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "PHOENIX_ENABLED=false\n"
                "PHOENIX_COLLECTOR_ENDPOINT=http://dotenv:6006\n"
                "PHOENIX_PROJECT_NAME=dotenv-project\n",
                encoding="utf-8",
            )
            config = PhoenixConfig.load(
                env_path=env_file,
                environ={
                    "PHOENIX_ENABLED": "true",
                    "PHOENIX_COLLECTOR_ENDPOINT": "http://process:6006",
                    "PHOENIX_PROJECT_NAME": "process-project",
                },
                enabled=False,
                endpoint="http://cli:6006",
                project_name="cli-project",
            )

        self.assertFalse(config.enabled)
        self.assertEqual(config.collector_endpoint, "http://cli:6006")
        self.assertEqual(config.project_name, "cli-project")

    def test_trace_records_bounded_tool_output_and_redacts_secret(self) -> None:
        provider = FakeProvider()
        tracer = FakeTracer()
        warnings: list[str] = []
        config = PhoenixConfig(api_key="top-secret", max_tool_output_bytes=8)
        observer = PhoenixObserver.create(
            config=config,
            case_id="case_0001",
            run_id="run_0001",
            model="gpt-5.6-sol",
            commit_message="message",
            diff="diff",
            prompt_sha256="p",
            runtime_config_sha256="r",
            history_tool_enabled=True,
            warn=warnings.append,
            telemetry_factory=lambda **_kwargs: (provider, tracer),
        )
        assert observer is not None
        observer.record_thread_start("thread-1")
        turn = observer.turn_start(name="initial", prompt="token=top-secret")
        item = SimpleNamespace(
            type="commandExecution",
            id="item-1",
            command="printf x",
            cwd=".",
            aggregated_output="abcdefghijk",
            exit_code=0,
            duration_ms=3,
        )
        observer.record_item(turn, item)
        result = SimpleNamespace(
            id="turn-1",
            final_response="done",
            usage=SimpleNamespace(
                total=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3)
            ),
        )
        observer.turn_end(turn, result)
        observer.record_validation(attempt=0, validation=validate_final_response("bad"))
        observer.complete(output="done", thread_id="thread-1", turn_ids=["turn-1"])

        command = next(span for span in tracer.spans if span.name == "tool.command")
        turn_span = next(span for span in tracer.spans if span.name == "codex.turn.initial")
        root_span = next(span for span in tracer.spans if span.name == "patchhound.experiments_codex_simple.run")
        self.assertIn("diff", str(root_span.attributes))
        self.assertIn("done", str(root_span.attributes))
        self.assertTrue(any(span.name == "codex.thread.start" for span in tracer.spans))
        self.assertTrue(any(span.name == "final.validation" for span in tracer.spans))
        self.assertEqual(command.attributes["patchhound.output_bytes"], 11)
        self.assertEqual(command.attributes["patchhound.output_stored_bytes"], 8)
        self.assertTrue(command.attributes["patchhound.output_truncated"])
        self.assertEqual(
            command.attributes["patchhound.output_sha256"],
            hashlib.sha256(b"abcdefghijk").hexdigest(),
        )
        self.assertNotIn("top-secret", str(turn_span.attributes))
        self.assertIn("[REDACTED]", str(turn_span.attributes))
        self.assertTrue(provider.flushed)
        self.assertTrue(provider.closed)
        self.assertEqual(warnings, [])

    def test_circuit_breaker_stops_after_first_failure(self) -> None:
        class Delegate:
            def __init__(self) -> None:
                self.calls = 0

            def export(self, _spans: object) -> SpanExportResult:
                self.calls += 1
                return SpanExportResult.FAILURE

            def shutdown(self) -> None:
                pass

            def force_flush(self, _timeout_millis: int) -> bool:
                return True

        delegate = Delegate()
        warnings: list[str] = []
        exporter = _CircuitBreakingExporter(delegate, warnings.append)
        self.assertEqual(exporter.export([]), SpanExportResult.FAILURE)
        self.assertEqual(exporter.export([]), SpanExportResult.FAILURE)
        self.assertEqual(delegate.calls, 1)
        self.assertEqual(len(warnings), 1)

    def test_trace_records_lifecycle_messages_reasoning_mcp_and_event_summary(self) -> None:
        tracer = FakeTracer()
        observer = PhoenixObserver.create(
            config=PhoenixConfig(),
            case_id="case_0001",
            run_id="run_0001",
            model="gpt-5.6-sol",
            commit_message="message",
            diff="diff",
            prompt_sha256="p",
            runtime_config_sha256="r",
            history_tool_enabled=True,
            warn=lambda _message: None,
            model_provider="openai",
            reasoning_effort="high",
            telemetry_factory=lambda **_kwargs: (FakeProvider(), tracer),
        )
        assert observer is not None
        observer.record_checkpoint(
            "runtime.configuration",
            input_value={"case": "case_0001"},
            output_value={"source_access": "read"},
        )
        turn = observer.turn_start(name="initial", prompt="prompt", timeout_seconds=30)
        observer.turn_created(turn, "turn-1")
        observer.record_item(
            turn,
            SimpleNamespace(
                type="reasoning",
                id="reasoning-1",
                summary=["bounded summary"],
                content=["must-not-be-recorded"],
            ),
            started_at_ms=100,
            completed_at_ms=120,
        )
        observer.record_item(
            turn,
            SimpleNamespace(
                type="mcpToolCall",
                id="mcp-1",
                server="patchhound_history",
                tool="query_pre_patch_history",
                arguments={"kind": "function_history"},
                result={"isError": False, "content": [{"type": "text", "text": "history"}]},
                error=None,
                status="completed",
                duration_ms=7,
            ),
            started_at_ms=121,
            completed_at_ms=128,
        )
        observer.record_item(
            turn,
            SimpleNamespace(type="agentMessage", id="message-1", text="final text", phase="final_answer"),
            started_at_ms=129,
            completed_at_ms=130,
        )
        usage = SimpleNamespace(
            total=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
            last=SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5),
            model_context_window=1000,
        )
        observer.record_token_update(turn, usage)
        result = SimpleNamespace(
            id="turn-1",
            status="completed",
            started_at=1,
            completed_at=2,
            duration_ms=1000,
            final_response="final text",
            items=[],
            usage=usage,
        )
        observer.turn_end(
            turn,
            result,
            event_summary=EventSummary(
                event_count=9,
                notification_counts=(("ItemCompletedNotification", 3),),
                assistant_delta_bytes=10,
                reasoning_delta_bytes=20,
                plan_delta_bytes=0,
            ),
        )
        observer.complete(output="final text", thread_id="thread-1", turn_ids=["turn-1"])

        names = [span.name for span in tracer.spans]
        self.assertIn("runtime.configuration", names)
        self.assertIn("codex.item.reasoning", names)
        self.assertIn("tool.patchhound_history.query_pre_patch_history", names)
        self.assertIn("codex.message.agent", names)
        self.assertIn("codex.token_usage.update", names)
        reasoning = next(span for span in tracer.spans if span.name == "codex.item.reasoning")
        mcp = next(
            span for span in tracer.spans if span.name == "tool.patchhound_history.query_pre_patch_history"
        )
        turn_span = next(span for span in tracer.spans if span.name == "codex.turn.initial")
        root_span = next(span for span in tracer.spans if span.name == "patchhound.experiments_codex_simple.run")
        self.assertNotIn("must-not-be-recorded", str(reasoning.attributes))
        self.assertFalse(reasoning.attributes["patchhound.reasoning_content_recorded"])
        self.assertEqual(mcp.start_time, 121_000_000)
        self.assertEqual(mcp.end_time, 128_000_000)
        self.assertEqual(mcp.attributes["patchhound.lifecycle_duration_ms"], 7)
        self.assertEqual(turn_span.attributes["patchhound.event_count"], 9)
        self.assertEqual(turn_span.attributes["patchhound.reasoning_delta_bytes"], 20)
        self.assertEqual(root_span.attributes["patchhound.item_count"], 3)

    def test_flush_and_shutdown_failures_do_not_escape(self) -> None:
        class FailingProvider:
            def force_flush(self, timeout_millis: int) -> bool:
                del timeout_millis
                raise RuntimeError("flush failed")

            def shutdown(self) -> None:
                raise RuntimeError("shutdown failed")

        warnings: list[str] = []
        observer = PhoenixObserver.create(
            config=PhoenixConfig(),
            case_id="case_0001",
            run_id="run_0001",
            model="gpt-5.6-sol",
            commit_message="message",
            diff="diff",
            prompt_sha256="p",
            runtime_config_sha256="r",
            history_tool_enabled=True,
            warn=warnings.append,
            telemetry_factory=lambda **_kwargs: (FailingProvider(), FakeTracer()),
        )
        assert observer is not None
        observer.complete(output="done", thread_id="thread-1", turn_ids=["turn-1"])
        self.assertGreaterEqual(len(warnings), 2)


if __name__ == "__main__":
    unittest.main()
