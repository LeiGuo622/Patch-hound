from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from expert_agent.config import PhoenixConfig  # noqa: E402
from expert_agent.phoenix import PhoenixObserver, _build_telemetry, normalize_collector_endpoint  # noqa: E402


class FakeSpan:
    def __init__(self, name: str, attributes: dict[str, object]) -> None:
        self.name = name
        self.attributes = dict(attributes)
        self.ended = False

    def get_span_context(self) -> SimpleNamespace:
        return SimpleNamespace(trace_id=1)

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def set_status(self, _status: object) -> None:
        pass

    def record_exception(self, _exc: BaseException) -> None:
        pass

    def end(self, **_kwargs: object) -> None:
        self.ended = True


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_span(self, name: str, **kwargs: object) -> FakeSpan:
        span = FakeSpan(name, dict(kwargs.get("attributes", {})))
        self.spans.append(span)
        return span


class FakeProvider:
    def force_flush(self, timeout_millis: int) -> bool:
        return timeout_millis == 1_000

    def shutdown(self) -> None:
        pass


class RecordingOtlpExporter:
    def __init__(self, *, endpoint: str, headers: dict[str, str], timeout: int) -> None:
        self.endpoint = endpoint
        self.headers = headers
        self.timeout = timeout

    def shutdown(self) -> None:
        pass

    def export(self, _spans: object) -> object:
        return object()


class PhoenixTest(unittest.TestCase):
    def test_defaults_are_expert_specific(self) -> None:
        config = PhoenixConfig.load(environ={})
        self.assertTrue(config.enabled)
        self.assertEqual(config.effective_project_name("gpt-5.6-sol"), "patchhound-experiments-codex-expert")
        self.assertEqual(config.effective_project_name("another-model"), "patchhound-experiments-codex-expert")
        self.assertEqual(config.max_tool_output_bytes, 131072)
        self.assertEqual(normalize_collector_endpoint("http://127.0.0.1:6006"), "http://127.0.0.1:6006/v1/traces")

    def test_trace_records_multiple_stage_threads_without_hidden_reasoning(self) -> None:
        tracer = FakeTracer()
        observer = PhoenixObserver.create(
            config=PhoenixConfig(),
            case_id="case_0001",
            run_id="run_0001",
            model="model",
            commit_message="message",
            diff="diff",
            prompt_sha256="prompt",
            runtime_config_sha256="runtime",
            history_tool_enabled=True,
            warn=lambda _message: None,
            telemetry_factory=lambda **_kwargs: (FakeProvider(), tracer),
        )
        assert observer is not None
        observer.record_thread_start("thread-1", stage_id="knowledge_selection")
        observer.record_thread_start("thread-2", stage_id="execution_flow")
        turn = observer.turn_start(name="execution_flow", prompt="prompt")
        observer.record_item(
            turn,
            SimpleNamespace(type="reasoning", id="r", summary=["summary"], content=["hidden content"]),
        )
        observer.complete(
            output="done",
            thread_ids={"knowledge_selection": "thread-1", "execution_flow": "thread-2"},
            turn_ids=["turn-1", "turn-2"],
        )
        root = next(span for span in tracer.spans if span.name == "patchhound.experiments_codex_expert.run")
        starts = [span for span in tracer.spans if span.name == "codex.thread.start"]
        reasoning = next(span for span in tracer.spans if span.name == "codex.item.reasoning")
        self.assertEqual([span.attributes["patchhound.stage_id"] for span in starts], [
            "knowledge_selection", "execution_flow"
        ])
        self.assertIn("thread-2", str(root.attributes["patchhound.thread_ids"]))
        self.assertNotIn("hidden content", str(reasoning.attributes))

    def test_telemetry_uses_resource_project_routing(self) -> None:
        created: list[RecordingOtlpExporter] = []

        def exporter_factory(*, endpoint: str, headers: dict[str, str], timeout: int) -> RecordingOtlpExporter:
            exporter = RecordingOtlpExporter(endpoint=endpoint, headers=headers, timeout=timeout)
            created.append(exporter)
            return exporter

        with patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            exporter_factory,
        ):
            provider, _tracer = _build_telemetry(
                endpoint="http://127.0.0.1:6006/v1/traces",
                project_name="patchhound-experiments-codex-expert",
                api_key="phoenix-secret",
                on_export_failure=lambda _message: None,
            )
        self.assertEqual(created[0].headers, {"Authorization": "Bearer phoenix-secret"})
        self.assertEqual(created[0].endpoint, "http://127.0.0.1:6006/v1/traces")
        self.assertEqual(
            provider.resource.attributes["service.name"],
            "patchhound-experiments-codex-expert",
        )
        self.assertEqual(
            provider.resource.attributes["openinference.project.name"],
            "patchhound-experiments-codex-expert",
        )
        provider.shutdown()


if __name__ == "__main__":
    unittest.main()
