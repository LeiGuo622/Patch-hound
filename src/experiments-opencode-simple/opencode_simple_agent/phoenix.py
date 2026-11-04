"""Fail-open Phoenix/OpenTelemetry tracing for OpenCode runs."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from .config import ConfigurationError, PhoenixConfig
from .events import NormalizedEvent, TurnExecution
from .validation import ValidationResult


class _SafeExporter(SpanExporter):
    def __init__(self, delegate: SpanExporter, on_failure: Callable[[str], None]) -> None:
        self.delegate = delegate
        self.on_failure = on_failure

    def export(self, spans: Any) -> SpanExportResult:
        try:
            result = self.delegate.export(spans)
            if result is not SpanExportResult.SUCCESS:
                self.on_failure(f"Phoenix exporter returned {result}")
            return result
        except Exception as exc:  # noqa: BLE001
            self.on_failure(f"Phoenix exporter failed: {exc.__class__.__name__}: {exc}")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self.delegate.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.on_failure(f"Phoenix exporter shutdown failed: {exc}")


class PhoenixObserver:
    def __init__(
        self,
        *,
        provider: TracerProvider,
        tracer: Any,
        root_span: Any,
        secrets: tuple[str, ...],
        max_output_bytes: int,
        warn: Callable[[str], None],
        project_name: str,
        model: str,
    ) -> None:
        self.provider = provider
        self.tracer = tracer
        self.root_span = root_span
        self.secrets = secrets
        self.max_output_bytes = max_output_bytes
        self.warn = warn
        self.project_name = project_name
        self.model = model
        self.available = True
        self.closed = False
        self.last_error: str | None = None
        self._turn_count = 0
        self._event_counts: Counter[str] = Counter()
        self._tool_counts: Counter[str] = Counter()
        self._turn_ids: list[str] = []
        self._assistant_delta_bytes = 0
        self._reasoning_delta_bytes = 0
        self._token_totals: dict[str, int] = {}

    @classmethod
    def create(
        cls,
        *,
        config: PhoenixConfig,
        case_id: str,
        run_id: str,
        model: str,
        prompt_sha256: str,
        input_hashes: dict[str, str],
        warn: Callable[[str], None],
        commit_message: str = "",
        diff: str = "",
        model_provider: str = "openai",
        reasoning_effort: str = "unknown",
        history_tool_enabled: bool = True,
        source_access: str = "read",
        scratch_access: str = "write",
        network_enabled: bool = True,
        exporter_factory: Callable[[str, dict[str, str]], SpanExporter] | None = None,
    ) -> PhoenixObserver | None:
        if not config.enabled:
            return None
        endpoint = normalize_collector_endpoint(config.collector_endpoint)
        secrets = config.secrets()
        try:
            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": config.project_name,
                        # Phoenix uses this OpenInference resource attribute to
                        # route OTLP spans into the named project. Without it,
                        # spans are accepted but silently appear in `default`.
                        "openinference.project.name": config.project_name,
                    }
                )
            )
            headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
            if exporter_factory is None:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                exporter: SpanExporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
            else:
                exporter = exporter_factory(endpoint, headers)
            observer_ref: list[PhoenixObserver | None] = [None]

            def export_failure(message: str) -> None:
                observer = observer_ref[0]
                if observer is not None:
                    observer.available = False
                    observer.last_error = _redact(message, secrets)[:1000]
                warn(_redact(message, secrets)[:1000])

            provider.add_span_processor(SimpleSpanProcessor(_SafeExporter(exporter, export_failure)))
            tracer = provider.get_tracer("patchhound.experiments_opencode_simple", "0.1.0")
            root = tracer.start_span(
                "patchhound.experiments_opencode_simple.run",
                attributes={
                    SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value,
                    SpanAttributes.SESSION_ID: f"{case_id}:{run_id}",
                    SpanAttributes.INPUT_VALUE: _redacted_json(
                        {"commit_message": commit_message, "diff": diff}, secrets
                    ),
                    SpanAttributes.INPUT_MIME_TYPE: "application/json",
                    SpanAttributes.LLM_MODEL_NAME: model,
                    SpanAttributes.METADATA: _redacted_json(
                        {
                            "agent_backend": "opencode",
                            "protocol": "jsonl-v1",
                            "case_id": case_id,
                            "run_id": run_id,
                            "model_provider": model_provider,
                            "model": model,
                            "reasoning_effort": reasoning_effort,
                            "prompt_sha256": prompt_sha256,
                            "source_access": source_access,
                            "scratch_access": scratch_access,
                            "network_enabled": network_enabled,
                            "history_tool_enabled": history_tool_enabled,
                            **input_hashes,
                        },
                        secrets,
                    ),
                    "patchhound.agent_backend": "opencode",
                    "patchhound.protocol": "jsonl-v1",
                    "patchhound.case_id": case_id,
                    "patchhound.run_id": run_id,
                    "patchhound.model_provider": model_provider,
                    "patchhound.reasoning_effort": reasoning_effort,
                    "patchhound.prompt_sha256": prompt_sha256,
                    "patchhound.runtime_config_sha256": input_hashes.get("runtime_config_sha256", ""),
                    "patchhound.commit_message_bytes": len(commit_message.encode("utf-8")),
                    "patchhound.diff_bytes": len(diff.encode("utf-8")),
                    "patchhound.source_access": source_access,
                    "patchhound.scratch_access": scratch_access,
                    "patchhound.network_enabled": network_enabled,
                    "patchhound.history_tool_enabled": history_tool_enabled,
                    "patchhound.phoenix_project": config.project_name,
                },
            )
            observer = cls(
                provider=provider,
                tracer=tracer,
                root_span=root,
                secrets=secrets,
                max_output_bytes=config.max_tool_output_bytes,
                warn=warn,
                project_name=config.project_name,
                model=model,
            )
            observer_ref[0] = observer
            return observer
        except Exception as exc:  # noqa: BLE001
            raise ConfigurationError(f"Phoenix initialization failed: {_redact(str(exc), secrets)[:500]}") from exc

    @property
    def trace_id(self) -> str | None:
        try:
            return f"{self.root_span.get_span_context().trace_id:032x}"
        except Exception:
            return None

    def checkpoint(
        self,
        name: str,
        *,
        input_value: Any | None = None,
        output_value: Any | None = None,
        attributes: dict[str, Any] | None = None,
        error: BaseException | None = None,
        parent: Any | None = None,
    ) -> None:
        if not self.available or self.closed:
            return
        try:
            values = {
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
                **(attributes or {}),
            }
            if input_value is not None:
                values[SpanAttributes.INPUT_VALUE] = self._json(input_value)
                values[SpanAttributes.INPUT_MIME_TYPE] = "application/json"
            if output_value is not None:
                values[SpanAttributes.OUTPUT_VALUE] = self._json(output_value)
                values[SpanAttributes.OUTPUT_MIME_TYPE] = "application/json"
            span = self.tracer.start_span(
                name,
                context=trace.set_span_in_context(parent or self.root_span),
                attributes=values,
            )
            if error is None:
                span.set_status(trace.Status(trace.StatusCode.OK))
            else:
                span.record_exception(error)
                span.set_status(trace.Status(trace.StatusCode.ERROR, _redact(str(error), self.secrets)[:500]))
            span.end()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Phoenix checkpoint {name} failed", exc)

    def turn_start(self, *, name: str, prompt: str, timeout_seconds: int | None = None) -> Any | None:
        if not self.available or self.closed:
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
                f"turn.{name}",
                context=trace.set_span_in_context(self.root_span),
                attributes=attributes,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Phoenix turn {name} failed", exc)
            return None

    def turn_created(self, handle: Any | None, turn_id: str) -> None:
        if handle is None:
            return
        try:
            handle.set_attribute("patchhound.turn_id", turn_id)
            if turn_id not in self._turn_ids:
                self._turn_ids.append(turn_id)
        except Exception as exc:  # noqa: BLE001
            self._fail("Phoenix turn id failed", exc)

    def turn_end(self, handle: Any | None, execution: TurnExecution) -> None:
        if handle is None:
            return
        try:
            response = execution.final_response
            usage = execution.usage
            handle.set_attribute(
                SpanAttributes.OUTPUT_VALUE,
                self._json(response),
            )
            handle.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/markdown")
            handle.set_attribute("patchhound.response_bytes", len(response.encode("utf-8")))
            handle.set_attribute("patchhound.finish_reason", execution.finish_reason or "unknown")
            handle.set_attribute("patchhound.provider", execution.provider or "unknown")
            handle.set_attribute("patchhound.model", execution.model or self.model)
            handle.set_attribute("patchhound.reasoning_effort", execution.reasoning_effort or "unknown")
            handle.set_attribute("patchhound.context_compacted", execution.context_compacted)
            _set_optional_int(handle, "patchhound.sdk_retry_count", execution.sdk_retry_count)
            handle.set_attribute("patchhound.event_count", len(execution.events))
            event_counts = Counter(event.type for event in execution.events)
            handle.set_attribute("patchhound.event_counts", self._json(dict(event_counts)))
            handle.set_attribute("patchhound.assistant_delta_bytes", self._assistant_delta_bytes)
            handle.set_attribute("patchhound.reasoning_delta_bytes", self._reasoning_delta_bytes)
            _set_optional_int(handle, SpanAttributes.LLM_TOKEN_COUNT_PROMPT, usage.get("input"))
            _set_optional_int(handle, SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, usage.get("output"))
            _set_optional_int(handle, SpanAttributes.LLM_TOKEN_COUNT_TOTAL, usage.get("total"))
            _set_optional_int(handle, "patchhound.reasoning_tokens", usage.get("reasoning"))
            _set_optional_int(handle, "patchhound.cache_read_tokens", usage.get("cache_read"))
            _set_optional_int(handle, "patchhound.cache_write_tokens", usage.get("cache_write"))
            for key in ("input", "output", "total", "reasoning", "cache_read", "cache_write"):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    self._token_totals[key] = self._token_totals.get(key, 0) + value
            handle.set_status(trace.Status(trace.StatusCode.OK))
            handle.end()
            self._turn_count += 1
        except Exception as exc:  # noqa: BLE001
            self._fail("Phoenix turn completion failed", exc)

    def turn_error(self, handle: Any | None, exc: BaseException) -> None:
        if handle is None:
            return
        try:
            handle.record_exception(exc)
            handle.set_status(
                trace.Status(trace.StatusCode.ERROR, _redact(f"{exc.__class__.__name__}: {exc}", self.secrets)[:500])
            )
            handle.end()
        except Exception as report_exc:  # noqa: BLE001
            self._fail("Phoenix turn failure recording failed", report_exc)

    def record_turn(
        self,
        execution: TurnExecution,
        validation: ValidationResult,
        *,
        prompt: str = "",
        timeout_seconds: int | None = None,
        turn_span: Any | None = None,
    ) -> None:
        self._assistant_delta_bytes = 0
        self._reasoning_delta_bytes = 0
        turn = turn_span or self.turn_start(name=execution.name, prompt=prompt, timeout_seconds=timeout_seconds)
        self.turn_created(turn, execution.turn_id)
        for event in execution.events:
            self.record_event(turn, execution.name, event)
        self.turn_end(turn, execution)
        self.record_validation(attempt=execution.name, validation=validation)

    def record_validation(self, *, attempt: str, validation: ValidationResult) -> None:
        self.checkpoint(
            f"validation.{attempt}",
            output_value={
                "ok": validation.ok,
                "assessment": validation.assessment,
                "confidence": validation.confidence,
                "errors": [issue.code for issue in validation.issues],
            },
            attributes={
                "patchhound.validation_attempt": attempt,
                "patchhound.validation_ok": validation.ok,
                "patchhound.final_assessment": validation.assessment or "unknown",
                "patchhound.confidence": validation.confidence or "unknown",
            },
        )

    def record_event(self, parent: Any | None, turn_name: str, event: NormalizedEvent) -> None:
        if not self.available or self.closed:
            return
        self._event_counts[event.type] += 1
        payload = dict(event.payload)
        if event.type == "assistant_delta":
            value = payload.get("text")
            if isinstance(value, str):
                self._assistant_delta_bytes += len(value.encode("utf-8"))
            return
        if event.type == "reasoning_delta":
            value = payload.get("bytes")
            if isinstance(value, int) and not isinstance(value, bool):
                self._reasoning_delta_bytes += value
            return
        if event.type in {"turn_started", "turn_finished"}:
            return
        attributes: dict[str, Any] = {
            "patchhound.event_type": event.type,
            "patchhound.event_timestamp": event.timestamp,
            "patchhound.turn_name": turn_name,
        }
        event_name = f"event.{event.type}"
        span_kind = OpenInferenceSpanKindValues.CHAIN.value
        success = True
        if event.type.startswith("tool_"):
            tool = str(payload.get("tool", "unknown"))
            event_name = f"tool.{tool}"
            span_kind = OpenInferenceSpanKindValues.TOOL.value
            attributes.update(
                {
                    SpanAttributes.TOOL_NAME: tool,
                    SpanAttributes.TOOL_ID: str(payload.get("part_id", "unknown")),
                    "patchhound.tool_type": tool,
                    "patchhound.tool_status": str(payload.get("status", "unknown")),
                }
            )
            if event.type == "tool_finished":
                success = payload.get("success") is True
                attributes["patchhound.tool_success"] = success
            self._tool_counts[tool] += 1
        elif event.type == "permission_denied":
            event_name = "permission.denied"
            success = False
        elif event.type == "assistant_delta":
            event_name = "assistant.delta"
        elif event.type == "reasoning_delta":
            event_name = "reasoning.delta"
        elif event.type == "token_usage":
            event_name = "token_usage.update"
        self._record_event_span(
            event_name,
            parent=parent,
            span_kind=span_kind,
            input_value={"turn": turn_name, "event": event.type},
            output_value=payload,
            attributes=attributes,
            success=success,
        )

    def _record_event_span(
        self,
        name: str,
        *,
        parent: Any | None,
        span_kind: str,
        input_value: Any,
        output_value: Any,
        attributes: dict[str, Any],
        success: bool,
    ) -> None:
        try:
            values = {
                SpanAttributes.OPENINFERENCE_SPAN_KIND: span_kind,
                SpanAttributes.INPUT_VALUE: self._json(input_value),
                SpanAttributes.INPUT_MIME_TYPE: "application/json",
                SpanAttributes.OUTPUT_VALUE: self._json(output_value),
                SpanAttributes.OUTPUT_MIME_TYPE: "application/json",
                **attributes,
            }
            span = self.tracer.start_span(
                name,
                context=trace.set_span_in_context(parent or self.root_span),
                attributes=values,
            )
            if success:
                span.set_status(trace.Status(trace.StatusCode.OK))
            else:
                span.set_status(trace.Status(trace.StatusCode.ERROR, "event reported failure"))
            span.end()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Phoenix event {name} failed", exc)

    def complete(
        self,
        *,
        status: str,
        output: str | None = None,
        error: BaseException | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.root_span.set_attribute("patchhound.status", status)
            self.root_span.set_attribute("patchhound.phoenix_project", self.project_name)
            self.root_span.set_attribute("patchhound.turn_count", self._turn_count)
            self.root_span.set_attribute("patchhound.turn_ids", self._turn_ids)
            self.root_span.set_attribute("patchhound.event_count", sum(self._event_counts.values()))
            self.root_span.set_attribute("patchhound.event_counts", self._json(dict(self._event_counts)))
            self.root_span.set_attribute("patchhound.item_count", sum(self._event_counts.values()))
            self.root_span.set_attribute("patchhound.item_counts", self._json(dict(self._event_counts)))
            self.root_span.set_attribute("patchhound.tool_counts", self._json(dict(self._tool_counts)))
            for key, attribute in (
                ("input", SpanAttributes.LLM_TOKEN_COUNT_PROMPT),
                ("output", SpanAttributes.LLM_TOKEN_COUNT_COMPLETION),
                ("total", SpanAttributes.LLM_TOKEN_COUNT_TOTAL),
                ("reasoning", "patchhound.reasoning_tokens"),
                ("cache_read", "patchhound.cache_read_tokens"),
                ("cache_write", "patchhound.cache_write_tokens"),
            ):
                if key in self._token_totals:
                    self.root_span.set_attribute(attribute, self._token_totals[key])
            for name, value in (attributes or {}).items():
                self.root_span.set_attribute(name, value)
            if output is not None:
                self.root_span.set_attribute(SpanAttributes.OUTPUT_VALUE, self._bounded(output))
                self.root_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/markdown")
            if error is None:
                self.root_span.set_status(trace.Status(trace.StatusCode.OK))
            else:
                self.root_span.record_exception(error)
                self.root_span.set_status(trace.Status(trace.StatusCode.ERROR, _redact(str(error), self.secrets)[:500]))
            self.root_span.end()
            self.provider.force_flush(timeout_millis=1000)
            self.provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.last_error = _redact(str(exc), self.secrets)[:1000]
            self.warn(f"Phoenix finalization failed: {self.last_error}")

    def _json(self, value: Any) -> str:
        return self._bounded(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))

    def _bounded(self, value: str) -> str:
        redacted = _redact(value, self.secrets)
        raw = redacted.encode("utf-8", errors="replace")
        if len(raw) <= self.max_output_bytes:
            return redacted
        return json.dumps(
            {
                "truncated": True,
                "raw_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            sort_keys=True,
        )

    def _fail(self, message: str, exc: BaseException) -> None:
        self.available = False
        self.last_error = _redact(str(exc), self.secrets)[:1000]
        self.warn(f"{message}: {self.last_error}")


def normalize_collector_endpoint(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"invalid Phoenix collector endpoint: {value}")
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        path = "/v1/traces"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(bearer\s+)?[^\s\"']+", r"\1[REDACTED]", result)
    return result


def _redacted_json(value: Any, secrets: tuple[str, ...]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _redact(raw, secrets)


def _set_optional_int(span: Any, name: str, value: Any) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        span.set_attribute(name, value)
