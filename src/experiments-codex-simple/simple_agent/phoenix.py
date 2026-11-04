"""Fail-open OpenTelemetry/OpenInference tracing for experiments-codex-simple."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from .artifacts import bounded_text
from .config import ConfigurationError, PhoenixConfig
from .validation import ValidationResult

TelemetryFactory = Callable[..., tuple[Any, Any]]


class PhoenixObserver:
    """Represent one Case run as a root Agent span with turn and tool children."""

    def __init__(
        self,
        *,
        provider: Any,
        tracer: Any,
        root_span: Any,
        model: str,
        secrets: tuple[str, ...],
        max_tool_output_bytes: int,
        warn: Callable[[str], None],
    ) -> None:
        self.provider = provider
        self.tracer = tracer
        self.root_span = root_span
        self.model = model
        self.secrets = secrets
        self.max_tool_output_bytes = max_tool_output_bytes
        self.warn = warn
        self._ingestion_failed = False
        self._warning_count = 0
        self._closed = False
        self._item_counts: Counter[str] = Counter()
        self._turn_count = 0

    @classmethod
    def create(
        cls,
        *,
        config: PhoenixConfig,
        case_id: str,
        run_id: str,
        model: str,
        commit_message: str,
        diff: str,
        prompt_sha256: str,
        runtime_config_sha256: str,
        history_tool_enabled: bool,
        warn: Callable[[str], None],
        model_provider: str = "openai",
        reasoning_effort: str = "unknown",
        telemetry_factory: TelemetryFactory | None = None,
    ) -> PhoenixObserver | None:
        if not config.enabled:
            return None
        endpoint = normalize_collector_endpoint(config.collector_endpoint)
        project_name = config.effective_project_name(model)
        secrets = config.secrets()
        pending_failures: list[str] = []
        observer_ref: list[PhoenixObserver | None] = [None]

        def export_failure(message: str) -> None:
            observer = observer_ref[0]
            if observer is None:
                pending_failures.append(message)
            else:
                observer._export_failure(message)

        factory = telemetry_factory or _build_telemetry
        try:
            provider, tracer = factory(
                endpoint=endpoint,
                project_name=project_name,
                api_key=config.api_key,
                on_export_failure=export_failure,
            )
            root_span = tracer.start_span(
                "patchhound.experiments_codex_simple.run",
                attributes={
                    SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value,
                    SpanAttributes.SESSION_ID: f"{case_id}:{run_id}",
                    SpanAttributes.INPUT_VALUE: _redacted_json(
                        {"commit_message": commit_message, "diff": diff}, secrets
                    ),
                    SpanAttributes.INPUT_MIME_TYPE: "application/json",
                    SpanAttributes.METADATA: _redacted_json(
                        {
                            "case_id": case_id,
                            "run_id": run_id,
                            "agent_backend": "codex_sdk",
                            "protocol": "codex_app_server",
                            "model_provider": model_provider,
                            "model": model,
                            "reasoning_effort": reasoning_effort,
                            "prompt_sha256": prompt_sha256,
                            "runtime_config_sha256": runtime_config_sha256,
                            "source_access": "read",
                            "scratch_access": "write",
                            "network_enabled": False,
                            "control_plane_network_enabled": True,
                            "history_tool_enabled": history_tool_enabled,
                        },
                        secrets,
                    ),
                    SpanAttributes.LLM_MODEL_NAME: model,
                    "patchhound.case_id": case_id,
                    "patchhound.run_id": run_id,
                    "patchhound.agent_backend": "codex_sdk",
                    "patchhound.protocol": "codex_app_server",
                    "patchhound.model_provider": model_provider,
                    "patchhound.reasoning_effort": reasoning_effort,
                    "patchhound.prompt_sha256": prompt_sha256,
                    "patchhound.runtime_config_sha256": runtime_config_sha256,
                    "patchhound.commit_message_bytes": len(commit_message.encode("utf-8")),
                    "patchhound.diff_bytes": len(diff.encode("utf-8")),
                },
            )
        except Exception as exc:  # noqa: BLE001 - the workflow turns setup failures into fail-open warnings.
            raise ConfigurationError(f"Phoenix initialization failed: {_redact(str(exc), secrets)[:500]}") from exc

        observer = cls(
            provider=provider,
            tracer=tracer,
            root_span=root_span,
            model=model,
            secrets=secrets,
            max_tool_output_bytes=config.max_tool_output_bytes,
            warn=warn,
        )
        observer_ref[0] = observer
        for failure in pending_failures:
            observer._export_failure(failure)
        return observer

    @property
    def trace_id(self) -> str:
        try:
            return f"{int(self.root_span.get_span_context().trace_id):032x}"
        except Exception:
            return "unknown"

    @property
    def healthy(self) -> bool:
        return not self._ingestion_failed

    @property
    def warning_count(self) -> int:
        return self._warning_count

    def set_thread_id(self, thread_id: str) -> None:
        if self._closed:
            return
        try:
            self.root_span.set_attribute("patchhound.thread_id", thread_id)
        except Exception as exc:  # noqa: BLE001
            self._warning("set thread id", exc)

    def record_thread_start(self, thread_id: str) -> None:
        if not self.healthy or self._closed:
            return
        try:
            span = self.tracer.start_span(
                "codex.thread.start",
                context=otel_trace.set_span_in_context(self.root_span),
                attributes={
                    SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
                    "patchhound.thread_id": thread_id,
                    "patchhound.ephemeral": True,
                },
            )
            _set_ok(span)
            span.end()
        except Exception as exc:  # noqa: BLE001
            self._warning("record thread start", exc)

    def operation_start(
        self,
        name: str,
        *,
        input_value: Any | None = None,
        attributes: dict[str, Any] | None = None,
        parent: Any | None = None,
    ) -> Any | None:
        """Start a host-side lifecycle span under the run or supplied parent."""

        if not self.healthy or self._closed:
            return None
        try:
            values: dict[str, Any] = {
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
                "patchhound.operation": name,
                **(attributes or {}),
            }
            if input_value is not None:
                values[SpanAttributes.INPUT_VALUE] = self._json(input_value)
                values[SpanAttributes.INPUT_MIME_TYPE] = "application/json"
            return self.tracer.start_span(
                name,
                context=otel_trace.set_span_in_context(parent or self.root_span),
                attributes=values,
            )
        except Exception as exc:  # noqa: BLE001
            self._warning(f"start {name}", exc)
            return None

    def operation_end(
        self,
        handle: Any | None,
        *,
        output_value: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if handle is None:
            return
        try:
            if output_value is not None:
                handle.set_attribute(SpanAttributes.OUTPUT_VALUE, self._json(output_value))
                handle.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
            for name, value in (attributes or {}).items():
                handle.set_attribute(name, value)
            _set_ok(handle)
            handle.end()
        except Exception as exc:  # noqa: BLE001
            self._warning("finish lifecycle span", exc)

    def operation_error(self, handle: Any | None, exc: BaseException) -> None:
        if handle is None:
            return
        try:
            _set_error_message(handle, self._redact(f"{exc.__class__.__name__}: {exc}")[:1000])
            try:
                handle.record_exception(exc)
            except Exception:
                pass
            handle.end()
        except Exception as report_exc:  # noqa: BLE001
            self._warning("record lifecycle failure", report_exc)

    def record_checkpoint(
        self,
        name: str,
        *,
        input_value: Any | None = None,
        output_value: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        handle = self.operation_start(name, input_value=input_value, attributes=attributes)
        self.operation_end(handle, output_value=output_value)

    def turn_start(self, *, name: str, prompt: str, timeout_seconds: int | None = None) -> Any | None:
        if not self.healthy or self._closed:
            return None
        try:
            attributes: dict[str, Any] = {
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
                SpanAttributes.INPUT_VALUE: self._json(prompt),
                SpanAttributes.INPUT_MIME_TYPE: "text/plain",
                SpanAttributes.LLM_MODEL_NAME: self.model,
                "patchhound.turn_name": name,
                "patchhound.prompt_bytes": len(prompt.encode("utf-8")),
            }
            if timeout_seconds is not None:
                attributes["patchhound.timeout_seconds"] = timeout_seconds
            return self.tracer.start_span(
                f"codex.turn.{name}",
                context=otel_trace.set_span_in_context(self.root_span),
                attributes=attributes,
            )
        except Exception as exc:  # noqa: BLE001
            self._warning("start turn span", exc)
            return None

    def turn_created(self, handle: Any | None, turn_id: str) -> None:
        if handle is None:
            return
        try:
            handle.set_attribute("patchhound.turn_id", turn_id)
        except Exception as exc:  # noqa: BLE001
            self._warning("set turn id", exc)

    def record_item(
        self,
        parent: Any | None,
        item: Any,
        *,
        started_at_ms: int | None = None,
        completed_at_ms: int | None = None,
        incomplete: bool = False,
    ) -> None:
        if parent is None or not self.healthy or self._closed:
            return
        item = item.root if hasattr(item, "root") else item
        item_type = str(getattr(item, "type", "unknown"))
        try:
            name, span_kind, input_value, output_value, success, attributes = self._item_values(item)
            if incomplete:
                success = False
                attributes["patchhound.lifecycle_incomplete"] = True
            self._item_counts[item_type] += 1
            if started_at_ms is not None:
                attributes["patchhound.started_at_ms"] = started_at_ms
            if completed_at_ms is not None:
                attributes["patchhound.completed_at_ms"] = completed_at_ms
            if started_at_ms is not None and completed_at_ms is not None:
                attributes["patchhound.lifecycle_duration_ms"] = max(0, completed_at_ms - started_at_ms)
            span_args: dict[str, Any] = {
                "context": otel_trace.set_span_in_context(parent),
                "attributes": {
                    SpanAttributes.OPENINFERENCE_SPAN_KIND: span_kind,
                    SpanAttributes.INPUT_VALUE: self._json(input_value),
                    SpanAttributes.INPUT_MIME_TYPE: "application/json",
                    SpanAttributes.OUTPUT_VALUE: self._json(output_value),
                    SpanAttributes.OUTPUT_MIME_TYPE: "application/json",
                    "patchhound.item_type": item_type,
                    "patchhound.item_id": str(getattr(item, "id", "unknown")),
                    **attributes,
                },
            }
            if span_kind == OpenInferenceSpanKindValues.TOOL.value:
                span_args["attributes"][SpanAttributes.TOOL_NAME] = name
                span_args["attributes"][SpanAttributes.TOOL_ID] = str(
                    getattr(item, "id", "unknown")
                )
            if started_at_ms is not None:
                span_args["start_time"] = started_at_ms * 1_000_000
            span = self.tracer.start_span(
                name,
                **span_args,
            )
            if success:
                _set_ok(span)
            else:
                _set_error_message(span, f"{item_type} did not complete successfully")
            if completed_at_ms is None:
                span.end()
            else:
                span.end(end_time=completed_at_ms * 1_000_000)
        except Exception as exc:  # noqa: BLE001
            self._warning("record item span", exc)

    def record_token_update(self, parent: Any | None, usage: Any) -> None:
        if parent is None or not self.healthy or self._closed:
            return
        try:
            total = getattr(usage, "total", None)
            last = getattr(usage, "last", None)
            attributes: dict[str, Any] = {
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
            }
            _add_token_attributes(attributes, "patchhound.total", total)
            _add_token_attributes(attributes, "patchhound.last", last)
            context_window = getattr(usage, "model_context_window", None)
            if isinstance(context_window, int):
                attributes["patchhound.model_context_window"] = context_window
            span = self.tracer.start_span(
                "codex.token_usage.update",
                context=otel_trace.set_span_in_context(parent),
                attributes=attributes,
            )
            _set_ok(span)
            span.end()
        except Exception as exc:  # noqa: BLE001
            self._warning("record token update", exc)

    def turn_end(self, handle: Any | None, result: Any, *, event_summary: Any | None = None) -> None:
        if handle is None:
            return
        try:
            handle.set_attribute("patchhound.turn_id", str(getattr(result, "id", "unknown")))
            handle.set_attribute(
                SpanAttributes.OUTPUT_VALUE,
                self._json(str(getattr(result, "final_response", "") or "")),
            )
            handle.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/markdown")
            response = str(getattr(result, "final_response", "") or "")
            handle.set_attribute("patchhound.response_bytes", len(response.encode("utf-8")))
            handle.set_attribute("patchhound.turn_status", _enum(getattr(result, "status", "unknown")))
            _set_optional_int(handle, "patchhound.started_at", result, "started_at")
            _set_optional_int(handle, "patchhound.completed_at", result, "completed_at")
            _set_optional_int(handle, "patchhound.duration_ms", result, "duration_ms")
            items = getattr(result, "items", ()) or ()
            handle.set_attribute("patchhound.item_count", len(items))
            handle.set_attribute(
                "patchhound.item_counts",
                self._json(dict(Counter(str(getattr(_root(item), "type", "unknown")) for item in items))),
            )
            if event_summary is not None:
                handle.set_attribute("patchhound.event_count", int(getattr(event_summary, "event_count", 0)))
                handle.set_attribute(
                    "patchhound.notification_counts",
                    self._json(dict(getattr(event_summary, "notification_counts", ()))),
                )
                handle.set_attribute(
                    "patchhound.assistant_delta_bytes",
                    int(getattr(event_summary, "assistant_delta_bytes", 0)),
                )
                handle.set_attribute(
                    "patchhound.reasoning_delta_bytes",
                    int(getattr(event_summary, "reasoning_delta_bytes", 0)),
                )
                handle.set_attribute(
                    "patchhound.plan_delta_bytes",
                    int(getattr(event_summary, "plan_delta_bytes", 0)),
                )
            usage = getattr(result, "usage", None)
            total = getattr(usage, "total", None)
            _set_optional_int(handle, SpanAttributes.LLM_TOKEN_COUNT_PROMPT, total, "input_tokens")
            _set_optional_int(handle, SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, total, "output_tokens")
            _set_optional_int(handle, SpanAttributes.LLM_TOKEN_COUNT_TOTAL, total, "total_tokens")
            _set_optional_int(handle, "patchhound.reasoning_output_tokens", total, "reasoning_output_tokens")
            self._turn_count += 1
            _set_ok(handle)
            handle.end()
        except Exception as exc:  # noqa: BLE001
            self._warning("finish turn span", exc)

    def turn_error(self, handle: Any | None, exc: BaseException) -> None:
        if handle is None:
            return
        try:
            _set_error_message(handle, self._redact(f"{exc.__class__.__name__}: {exc}")[:1000])
            handle.end()
        except Exception as report_exc:  # noqa: BLE001
            self._warning("record turn failure", report_exc)

    def record_validation(self, *, attempt: int, validation: ValidationResult) -> None:
        if not self.healthy or self._closed:
            return
        try:
            span = self.tracer.start_span(
                "final.validation",
                context=otel_trace.set_span_in_context(self.root_span),
                attributes={
                    SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
                    "patchhound.validation_attempt": attempt,
                    "patchhound.validation_ok": validation.ok,
                    "patchhound.validation_issue_codes": [issue.code for issue in validation.issues],
                    "patchhound.final_assessment": validation.assessment or "unknown",
                    "patchhound.confidence": validation.confidence or "unknown",
                },
            )
            if validation.ok:
                _set_ok(span)
            else:
                _set_error_message(span, "final response contract validation failed")
            span.end()
        except Exception as exc:  # noqa: BLE001
            self._warning("record validation span", exc)

    def complete(self, *, output: str, thread_id: str, turn_ids: list[str]) -> None:
        if self._closed:
            return
        try:
            self.root_span.set_attribute(SpanAttributes.OUTPUT_VALUE, self._json(output))
            self.root_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/markdown")
            self.root_span.set_attribute("patchhound.status", "completed")
            self.root_span.set_attribute("patchhound.thread_id", thread_id)
            self.root_span.set_attribute("patchhound.turn_ids", turn_ids)
            self.root_span.set_attribute("patchhound.turn_count", self._turn_count)
            self.root_span.set_attribute("patchhound.item_count", sum(self._item_counts.values()))
            self.root_span.set_attribute("patchhound.item_counts", self._json(dict(self._item_counts)))
            _set_ok(self.root_span)
            self.root_span.end()
            self.flush()
        except Exception as exc:  # noqa: BLE001
            self._warning("complete trace", exc)
        finally:
            self.close()

    def fail(self, *, stage: str, exc: BaseException) -> None:
        if self._closed:
            return
        try:
            self.root_span.set_attribute(
                SpanAttributes.OUTPUT_VALUE,
                self._json({"error": f"{exc.__class__.__name__}: {exc}"}),
            )
            self.root_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
            self.root_span.set_attribute("patchhound.status", "failed")
            self.root_span.set_attribute("patchhound.failed_stage", stage)
            _set_error_message(self.root_span, self._redact(f"{exc.__class__.__name__}: {exc}")[:1000])
            self.root_span.end()
            self.flush()
        except Exception as report_exc:  # noqa: BLE001
            self._warning("fail trace", report_exc)
        finally:
            self.close()

    def flush(self) -> None:
        if self._closed:
            return
        try:
            if not self.provider.force_flush(timeout_millis=10_000):
                self._warning("flush", RuntimeError("OpenTelemetry force_flush returned false"))
        except Exception as exc:  # noqa: BLE001
            self._warning("flush", exc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            self._warning("shutdown", exc)

    def _item_values(self, item: Any) -> tuple[str, str, Any, Any, bool, dict[str, Any]]:
        item_type = str(getattr(item, "type", "unknown"))
        if item_type == "commandExecution":
            output = str(getattr(item, "aggregated_output", "") or "")
            bounded = bounded_text(output, self.max_tool_output_bytes)
            return (
                "tool.command",
                OpenInferenceSpanKindValues.TOOL.value,
                {
                    "command": getattr(item, "command", ""),
                    "cwd": str(getattr(item, "cwd", "")),
                    "actions": _jsonable(getattr(item, "command_actions", ())),
                    "source": _enum(getattr(item, "source", "unknown")),
                },
                {"text": bounded.text},
                getattr(item, "exit_code", None) == 0,
                {
                    "patchhound.tool_type": item_type,
                    "patchhound.exit_code": getattr(item, "exit_code", -1)
                    if getattr(item, "exit_code", None) is not None
                    else -1,
                    "patchhound.duration_ms": getattr(item, "duration_ms", -1)
                    if getattr(item, "duration_ms", None) is not None
                    else -1,
                    "patchhound.output_bytes": bounded.raw_bytes,
                    "patchhound.output_stored_bytes": bounded.stored_bytes,
                    "patchhound.output_truncated": bounded.truncated,
                    "patchhound.output_sha256": bounded.sha256,
                },
            )
        if item_type == "mcpToolCall":
            raw_output = json.dumps(_jsonable(getattr(item, "result", None)), ensure_ascii=False, sort_keys=True)
            bounded = bounded_text(raw_output, self.max_tool_output_bytes)
            server = str(getattr(item, "server", "?"))
            tool = str(getattr(item, "tool", "?"))
            status = _enum(getattr(item, "status", ""))
            return (
                f"tool.{server}.{tool}",
                OpenInferenceSpanKindValues.TOOL.value,
                _jsonable(getattr(item, "arguments", None)),
                {
                    "text": bounded.text,
                    "error": _jsonable(getattr(item, "error", None)),
                },
                status.lower() in {"completed", "success", "succeeded", "ok"},
                {
                    "patchhound.tool_type": item_type,
                    "patchhound.mcp_server": server,
                    "patchhound.mcp_tool": tool,
                    "patchhound.mcp_status": status,
                    "patchhound.duration_ms": getattr(item, "duration_ms", -1)
                    if getattr(item, "duration_ms", None) is not None
                    else -1,
                    "patchhound.output_bytes": bounded.raw_bytes,
                    "patchhound.output_stored_bytes": bounded.stored_bytes,
                    "patchhound.output_truncated": bounded.truncated,
                    "patchhound.output_sha256": bounded.sha256,
                },
            )
        if item_type == "fileChange":
            changes = _jsonable(getattr(item, "changes", ()))
            bounded = _bounded_json(changes, self.max_tool_output_bytes)
            status = _enum(getattr(item, "status", ""))
            return (
                "tool.file_change",
                OpenInferenceSpanKindValues.TOOL.value,
                {"change_count": len(getattr(item, "changes", ()) or ())},
                {"text": bounded.text},
                status.lower() in {"completed", "success", "succeeded", "ok"},
                _bounded_attributes(item_type, bounded, {"patchhound.file_change_status": status}),
            )
        if item_type == "dynamicToolCall":
            output = _jsonable(getattr(item, "content_items", None))
            bounded = _bounded_json(output, self.max_tool_output_bytes)
            success = getattr(item, "success", None)
            status = _enum(getattr(item, "status", ""))
            return (
                f"tool.dynamic.{getattr(item, 'tool', 'unknown')}",
                OpenInferenceSpanKindValues.TOOL.value,
                _jsonable(getattr(item, "arguments", None)),
                {"text": bounded.text},
                success is True or status.lower() in {"completed", "success", "succeeded", "ok"},
                _bounded_attributes(item_type, bounded, {"patchhound.dynamic_status": status}),
            )
        if item_type == "webSearch":
            output = _jsonable(getattr(item, "action", None))
            bounded = _bounded_json(output, self.max_tool_output_bytes)
            return (
                "tool.web_search",
                OpenInferenceSpanKindValues.TOOL.value,
                {"query": getattr(item, "query", "")},
                {"text": bounded.text},
                True,
                _bounded_attributes(item_type, bounded),
            )
        if item_type == "agentMessage":
            text = str(getattr(item, "text", "") or "")
            bounded = bounded_text(text, self.max_tool_output_bytes)
            return (
                "codex.message.agent",
                OpenInferenceSpanKindValues.LLM.value,
                {"phase": _enum(getattr(item, "phase", "unknown"))},
                {"text": bounded.text},
                True,
                _bounded_attributes(item_type, bounded),
            )
        if item_type == "reasoning":
            summary = _jsonable(getattr(item, "summary", ()))
            bounded = _bounded_json(summary, self.max_tool_output_bytes)
            return (
                "codex.item.reasoning",
                OpenInferenceSpanKindValues.CHAIN.value,
                {"content_parts": len(getattr(item, "content", ()) or ())},
                {"summary": bounded.text},
                True,
                _bounded_attributes(
                    item_type,
                    bounded,
                    {"patchhound.reasoning_content_recorded": False},
                ),
            )
        if item_type == "plan":
            text = str(getattr(item, "text", "") or "")
            bounded = bounded_text(text, self.max_tool_output_bytes)
            return (
                "codex.item.plan",
                OpenInferenceSpanKindValues.CHAIN.value,
                {},
                {"text": bounded.text},
                True,
                _bounded_attributes(item_type, bounded),
            )
        raw = _jsonable(item)
        bounded = _bounded_json(raw, self.max_tool_output_bytes)
        return (
            f"codex.item.{_snake_case(item_type)}",
            OpenInferenceSpanKindValues.CHAIN.value,
            {"type": item_type},
            {"text": bounded.text},
            True,
            _bounded_attributes(item_type, bounded),
        )

    def _json(self, value: Any) -> str:
        return _redacted_json(value, self.secrets)

    def _redact(self, value: str) -> str:
        return _redact(value, self.secrets)

    def _warning(self, operation: str, exc: BaseException) -> None:
        if operation == "export":
            if self._ingestion_failed:
                return
            self._ingestion_failed = True
        self._warning_count += 1
        self.warn(f"Phoenix warning during {operation}: {self._redact(str(exc))[:500]}")

    def _export_failure(self, message: str) -> None:
        self._warning("export", RuntimeError(message))


class _CircuitBreakingExporter(SpanExporter):
    def __init__(self, delegate: SpanExporter, on_failure: Callable[[str], None]) -> None:
        self.delegate = delegate
        self.on_failure = on_failure
        self.failed = False

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        if self.failed:
            return SpanExportResult.FAILURE
        try:
            result = self.delegate.export(spans)
        except Exception as exc:  # noqa: BLE001
            self.failed = True
            self.on_failure(f"{exc.__class__.__name__}: {exc}")
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self.failed = True
            self.on_failure("collector rejected an OTLP span batch")
        return result

    def shutdown(self) -> None:
        self.delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.delegate.force_flush(timeout_millis)


def _build_telemetry(
    *,
    endpoint: str,
    project_name: str,
    api_key: str | None,
    on_export_failure: Callable[[str], None],
) -> tuple[TracerProvider, Any]:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    headers = {"x-project-name": project_name}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    delegate = OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=10)
    exporter = _CircuitBreakingExporter(delegate, on_export_failure)
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "patchhound-experiments-codex-simple",
                "openinference.project.name": project_name,
            }
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, provider.get_tracer("patchhound.experiments_codex_simple", "0.1.0")


def normalize_collector_endpoint(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("PHOENIX_COLLECTOR_ENDPOINT must be an http(s) URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1/traces"):
        path += "/v1/traces"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _redacted_json(value: Any, secrets: tuple[str, ...]) -> str:
    return _redact(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, default=str), secrets)


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(by_alias=True, mode="json", exclude_none=True)
        except TypeError:
            return value.model_dump()
    if hasattr(value, "root"):
        return _jsonable(value.root)
    return str(value)


def _bounded_json(value: Any, max_bytes: int) -> Any:
    raw = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, default=str)
    return bounded_text(raw, max_bytes)


def _bounded_attributes(
    item_type: str,
    bounded: Any,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "patchhound.tool_type": item_type,
        "patchhound.output_bytes": bounded.raw_bytes,
        "patchhound.output_stored_bytes": bounded.stored_bytes,
        "patchhound.output_truncated": bounded.truncated,
        "patchhound.output_sha256": bounded.sha256,
        **(extra or {}),
    }


def _root(value: Any) -> Any:
    return value.root if hasattr(value, "root") else value


def _snake_case(value: str) -> str:
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
    return re.sub(r"[^a-z0-9_]+", "_", normalized).strip("_") or "unknown"


def _add_token_attributes(attributes: dict[str, Any], prefix: str, value: Any) -> None:
    for field_name in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        field = getattr(value, field_name, None)
        if isinstance(field, int):
            attributes[f"{prefix}_{field_name}"] = field


def _set_optional_int(span: Any, attribute: str, value: Any, field_name: str) -> None:
    field = getattr(value, field_name, None)
    if isinstance(field, int):
        span.set_attribute(attribute, field)


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value))


def _set_ok(span: Any) -> None:
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.OK))


def _set_error_message(span: Any, message: str) -> None:
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.ERROR, message))
