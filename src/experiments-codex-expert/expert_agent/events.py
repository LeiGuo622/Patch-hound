"""Streaming Codex turn execution and event recording."""

from __future__ import annotations

import queue
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import RunArtifacts


@dataclass(frozen=True)
class EventSummary:
    event_count: int
    notification_counts: tuple[tuple[str, int], ...]
    assistant_delta_bytes: int
    reasoning_delta_bytes: int
    plan_delta_bytes: int


@dataclass(frozen=True)
class TurnExecution:
    result: Any
    events: tuple[Any, ...]
    event_summary: EventSummary = EventSummary(0, (), 0, 0, 0)

    @property
    def final_response(self) -> str:
        return str(getattr(self.result, "final_response", "") or "")

    @property
    def turn_id(self) -> str:
        return str(getattr(self.result, "id", "unknown"))


class EventReporter:
    def __init__(
        self,
        *,
        live_output: bool,
        artifacts: RunArtifacts,
        phoenix: Any | None,
        turn_span: Any | None,
    ) -> None:
        self.live_output = live_output
        self.artifacts = artifacts
        self.phoenix = phoenix
        self.turn_span = turn_span
        self.events: list[Any] = []
        self._stream_open = False
        self._delta_item_ids: set[str] = set()
        self._started_items: dict[str, tuple[Any, int]] = {}
        self._notification_counts: Counter[str] = Counter()
        self._assistant_delta_bytes = 0
        self._reasoning_delta_bytes = 0
        self._plan_delta_bytes = 0

    def status(self, message: str) -> None:
        if not self.live_output:
            return
        self._close_stream()
        print(f"[patchhound-expert] {message}", flush=True)

    def event(self, event: Any) -> None:
        self.events.append(event)
        payload = getattr(event, "payload", event)
        name = payload.__class__.__name__
        self._notification_counts[name] += 1
        if name == "AgentMessageDeltaNotification":
            self._delta_item_ids.add(str(getattr(payload, "item_id", "")))
            self._assistant_delta_bytes += len(str(getattr(payload, "delta", "")).encode("utf-8"))
            if self.live_output:
                if not self._stream_open:
                    print("[patchhound-expert] assistant output:", flush=True)
                    self._stream_open = True
                print(str(getattr(payload, "delta", "")), end="", flush=True)
            return

        if name in {"ReasoningSummaryTextDeltaNotification", "ReasoningTextDeltaNotification"}:
            self._reasoning_delta_bytes += len(str(getattr(payload, "delta", "")).encode("utf-8"))
            return

        if name == "PlanDeltaNotification":
            self._plan_delta_bytes += len(str(getattr(payload, "delta", "")).encode("utf-8"))
            return

        if name == "ItemCompletedNotification":
            item = _item_root(getattr(payload, "item", None))
            self.artifacts.record_item(item)
            if self.phoenix is not None:
                item_id = str(getattr(item, "id", "unknown"))
                started = self._started_items.pop(item_id, None)
                self.phoenix.record_item(
                    self.turn_span,
                    item,
                    started_at_ms=started[1] if started is not None else None,
                    completed_at_ms=_int_or_none(payload, "completed_at_ms"),
                )
            if self.live_output:
                message = _format_completed_item(item)
                if message:
                    self.status(message)
                if getattr(item, "type", None) == "agentMessage":
                    item_id = str(getattr(item, "id", ""))
                    text = str(getattr(item, "text", "") or "")
                    if text and item_id not in self._delta_item_ids:
                        self.status("assistant output:")
                        print(text, flush=True)
            return

        if name == "ItemStartedNotification":
            item = _item_root(getattr(payload, "item", None))
            item_id = str(getattr(item, "id", "unknown"))
            started_at_ms = _int_or_none(payload, "started_at_ms")
            if started_at_ms is not None:
                self._started_items[item_id] = (item, started_at_ms)
            if self.live_output:
                message = _format_started_item(item)
                if message:
                    self.status(message)
            return

        if name == "ThreadTokenUsageUpdatedNotification":
            if self.phoenix is not None:
                self.phoenix.record_token_update(self.turn_span, getattr(payload, "token_usage", None))
            return

        if name == "TurnCompletedNotification" and self.live_output:
            turn = getattr(payload, "turn", None)
            self.status(
                f"turn completed: status={_enum(getattr(turn, 'status', 'unknown'))} "
                f"duration_ms={getattr(turn, 'duration_ms', None)}"
            )

    def close(self) -> None:
        self._close_stream()
        if self.phoenix is not None:
            for item, started_at_ms in self._started_items.values():
                self.phoenix.record_item(
                    self.turn_span,
                    item,
                    started_at_ms=started_at_ms,
                    incomplete=True,
                )
        self._started_items.clear()

    def summary(self) -> EventSummary:
        return EventSummary(
            event_count=sum(self._notification_counts.values()),
            notification_counts=tuple(sorted(self._notification_counts.items())),
            assistant_delta_bytes=self._assistant_delta_bytes,
            reasoning_delta_bytes=self._reasoning_delta_bytes,
            plan_delta_bytes=self._plan_delta_bytes,
        )

    def _close_stream(self) -> None:
        if self._stream_open:
            print(flush=True)
            self._stream_open = False


def run_turn_with_timeout(
    *,
    codex: Any,
    thread: Any,
    prompt: str,
    source_dir: Path,
    approval_mode: Any,
    model: str | None,
    timeout_seconds: int,
    turn_name: str,
    live_output: bool,
    artifacts: RunArtifacts,
    phoenix: Any | None,
    turn_parent: Any | None = None,
) -> TurnExecution:
    if timeout_seconds <= 0:
        raise TimeoutError("no time remains for the Codex turn")
    turn_span = (
        phoenix.turn_start(name=turn_name, prompt=prompt, timeout_seconds=timeout_seconds, parent=turn_parent)
        if phoenix is not None
        else None
    )
    reporter = EventReporter(
        live_output=live_output,
        artifacts=artifacts,
        phoenix=phoenix,
        turn_span=turn_span,
    )

    def run() -> TurnExecution:
        return _run_turn_streaming(
            thread=thread,
            prompt=prompt,
            source_dir=source_dir,
            approval_mode=approval_mode,
            model=model,
            reporter=reporter,
        )

    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            results.put(("ok", run()))
        except BaseException as exc:  # noqa: BLE001 - preserve SDK failures at the boundary.
            results.put(("error", exc))

    worker_thread = threading.Thread(target=worker, name="patchhound-expert-codex-turn", daemon=True)
    worker_thread.start()
    try:
        status, payload = results.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        codex.close()
        error = TimeoutError(f"timed out waiting for Codex turn after {timeout_seconds} seconds")
        if phoenix is not None:
            phoenix.turn_error(turn_span, error)
        raise error from exc
    finally:
        reporter.close()

    if status == "error":
        if phoenix is not None:
            phoenix.turn_error(turn_span, payload)
        raise payload
    execution: TurnExecution = payload
    if phoenix is not None:
        phoenix.turn_end(turn_span, execution.result, event_summary=execution.event_summary)
    return execution


def _run_turn_streaming(
    *,
    thread: Any,
    prompt: str,
    source_dir: Path,
    approval_mode: Any,
    model: str | None,
    reporter: EventReporter,
) -> TurnExecution:
    turn = thread.turn(
        prompt,
        approval_mode=approval_mode.deny_all,
        cwd=str(source_dir),
        model=model,
    )
    reporter.artifacts.record("codex_sdk", "turn_start", True, f"turn_id={turn.id}")
    reporter.status(f"turn_id={turn.id}")
    if reporter.phoenix is not None:
        reporter.phoenix.turn_created(reporter.turn_span, str(turn.id))
    stream = turn.stream()
    try:
        result = _collect_turn_result(stream, turn.id, reporter)
    finally:
        stream.close()
    return TurnExecution(result=result, events=tuple(reporter.events), event_summary=reporter.summary())


def _collect_turn_result(stream: Any, turn_id: str, reporter: EventReporter) -> Any:
    from openai_codex._run import TurnResult, _final_assistant_response_from_items, _raise_for_failed_turn
    from openai_codex.generated.v2_all import (
        ItemCompletedNotification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
    )

    completed: Any | None = None
    items: list[Any] = []
    usage: Any | None = None
    for event in stream:
        reporter.event(event)
        payload = event.payload
        if isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn_id:
            items.append(payload.item)
        elif isinstance(payload, ThreadTokenUsageUpdatedNotification) and payload.turn_id == turn_id:
            usage = payload.token_usage
        elif isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn_id:
            completed = payload
    if completed is None:
        raise RuntimeError("turn completed event not received")
    _raise_for_failed_turn(completed.turn)
    turn = completed.turn
    return TurnResult(
        id=turn.id,
        status=turn.status,
        error=turn.error,
        started_at=turn.started_at,
        completed_at=turn.completed_at,
        duration_ms=turn.duration_ms,
        final_response=_final_assistant_response_from_items(items),
        items=items,
        usage=usage,
    )


def _item_root(item: Any) -> Any:
    return item.root if hasattr(item, "root") else item


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value))


def _format_started_item(item: Any) -> str | None:
    item_type = getattr(item, "type", None)
    if item_type == "commandExecution":
        return f"command started: {_one_line(str(getattr(item, 'command', '')), 140)}"
    if item_type == "mcpToolCall":
        return f"mcp tool started: {getattr(item, 'server', '?')}.{getattr(item, 'tool', '?')}"
    if item_type == "fileChange":
        return "file change started"
    return None


def _format_completed_item(item: Any) -> str | None:
    item_type = getattr(item, "type", None)
    if item_type == "commandExecution":
        return (
            f"command completed: exit_code={getattr(item, 'exit_code', None)} "
            f"duration_ms={getattr(item, 'duration_ms', None)} "
            f"command={_one_line(str(getattr(item, 'command', '')), 100)}"
        )
    if item_type == "mcpToolCall":
        return (
            f"mcp tool completed: {getattr(item, 'server', '?')}.{getattr(item, 'tool', '?')} "
            f"status={_enum(getattr(item, 'status', None))}"
        )
    if item_type == "fileChange":
        return f"file change completed: status={_enum(getattr(item, 'status', None))}"
    return None


def _one_line(value: str, max_length: int) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 3] + "..."


def _int_or_none(value: Any, field_name: str) -> int | None:
    field = getattr(value, field_name, None)
    return field if isinstance(field, int) else None
