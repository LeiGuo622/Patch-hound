"""Host-owned run artifacts for experiments-codex-simple."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .case_io import RunPaths

DEFAULT_EVENT_OUTPUT_BYTES = 128 * 1024


@dataclass(frozen=True)
class ProcessEntry:
    timestamp: str
    component: str
    action: str
    success: bool
    detail: str


@dataclass(frozen=True)
class TokenEntry:
    turn: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class ConversationTurn:
    name: str
    prompt: str
    response: str
    events: tuple[str, ...]


@dataclass
class RunArtifacts:
    paths: RunPaths
    save_conversation: bool = False
    process_entries: list[ProcessEntry] = field(default_factory=list)
    token_entries: list[TokenEntry] = field(default_factory=list)
    conversation_turns: list[ConversationTurn] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    def initialize_manifest(self, values: dict[str, Any]) -> None:
        self.manifest = {"schema_version": 1, **values}
        self.write_manifest()

    def update_manifest(self, **values: Any) -> None:
        self.manifest.update(values)
        self.write_manifest()

    def record(
        self,
        component: str,
        action: str,
        success: bool = True,
        detail: str = "",
    ) -> None:
        self.process_entries.append(
            ProcessEntry(
                timestamp=utc_now(),
                component=component,
                action=action,
                success=success,
                detail=" ".join(str(detail).split())[:4000],
            )
        )

    def record_item(self, item: Any) -> None:
        item = item.root if hasattr(item, "root") else item
        item_type = str(getattr(item, "type", "unknown"))
        if item_type == "commandExecution":
            output = str(getattr(item, "aggregated_output", "") or "")
            payload = bounded_text(output, DEFAULT_EVENT_OUTPUT_BYTES)
            detail = (
                f"exit_code={getattr(item, 'exit_code', None)} "
                f"duration_ms={getattr(item, 'duration_ms', None)} "
                f"output_bytes={payload.raw_bytes} truncated={payload.truncated} "
                f"sha256={payload.sha256} command={getattr(item, 'command', '')}"
            )
            self.record("commandExecution", str(getattr(item, "command", "command")), _item_success(item), detail)
        elif item_type == "mcpToolCall":
            result = json.dumps(_jsonable(getattr(item, "result", None)), ensure_ascii=False, sort_keys=True)
            payload = bounded_text(result, DEFAULT_EVENT_OUTPUT_BYTES)
            name = f"{getattr(item, 'server', '?')}.{getattr(item, 'tool', '?')}"
            detail = (
                f"duration_ms={getattr(item, 'duration_ms', None)} "
                f"output_bytes={payload.raw_bytes} truncated={payload.truncated} sha256={payload.sha256}"
            )
            self.record("mcpToolCall", name, _item_success(item), detail)
        elif item_type == "fileChange":
            changes = getattr(item, "changes", ()) or ()
            self.record("fileChange", f"changes={len(changes)}", _item_success(item), f"status={_status(item)}")

    def record_tokens(self, turn_name: str, result: Any) -> None:
        usage = getattr(result, "usage", None)
        total = getattr(usage, "total", None)
        self.token_entries.append(
            TokenEntry(
                turn=turn_name,
                input_tokens=_int_or_none(total, "input_tokens"),
                output_tokens=_int_or_none(total, "output_tokens"),
                reasoning_output_tokens=_int_or_none(total, "reasoning_output_tokens"),
                total_tokens=_int_or_none(total, "total_tokens"),
            )
        )

    def record_conversation(self, turn_name: str, prompt: str, result: Any, events: tuple[Any, ...]) -> None:
        if not self.save_conversation:
            return
        serialized: list[str] = []
        for event in events:
            payload = getattr(event, "payload", event)
            raw = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, default=str)
            bounded = bounded_text(raw, DEFAULT_EVENT_OUTPUT_BYTES)
            if bounded.truncated:
                raw = (
                    f"{bounded.text}\n[TRUNCATED raw_bytes={bounded.raw_bytes} "
                    f"sha256={bounded.sha256}]"
                )
            serialized.append(raw)
        self.conversation_turns.append(
            ConversationTurn(
                name=turn_name,
                prompt=prompt,
                response=str(getattr(result, "final_response", "") or ""),
                events=tuple(serialized),
            )
        )

    def write_model_config(self, values: dict[str, Any]) -> None:
        lines = ["# Model Configuration", ""]
        for label, value in values.items():
            lines.append(f"- {label}: `{_inline(value)}`")
        atomic_write_text(self.paths.model_config, "\n".join(lines) + "\n")

    def write_manifest(self) -> None:
        atomic_write_text(
            self.paths.manifest,
            json.dumps(self.manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        )

    def write_analysis(self, text: str) -> None:
        atomic_write_text(self.paths.analysis, text.rstrip() + "\n")

    def write_invalid_response(self, text: str) -> None:
        atomic_write_text(self.paths.invalid_response, text.rstrip() + "\n")

    def ensure_history_log(self, *, enabled: bool) -> None:
        """Materialize an explicit zero-query log without replacing real MCP evidence."""

        if not enabled or self.paths.history_queries.is_file():
            return
        atomic_write_text(
            self.paths.history_queries,
            "# Pre-Patch History Queries\n\n"
            "No persisted history queries were recorded for this run.\n",
        )
        self.record("artifact", "history_queries", True, "zero persisted queries")

    def write_logs(self) -> None:
        self._write_process_log()
        self._write_token_usage()
        if self.save_conversation:
            self._write_conversation()

    def _write_process_log(self) -> None:
        lines = [
            "# Process Log",
            "",
            "Host-recorded Codex thread, turn, tool, validation, artifact, and telemetry events.",
            "",
            "| # | Time (UTC) | Component | Action | Success | Detail |",
            "|---:|---|---|---|---|---|",
        ]
        if not self.process_entries:
            lines.append("| 1 | - | runner | no entries | yes | - |")
        for index, entry in enumerate(self.process_entries, 1):
            lines.append(
                f"| {index} | {_cell(entry.timestamp)} | {_cell(entry.component)} | {_cell(entry.action)} | "
                f"{'yes' if entry.success else 'no'} | {_cell(entry.detail or '-')} |"
            )
        atomic_write_text(self.paths.process_log, "\n".join(lines) + "\n")

    def _write_token_usage(self) -> None:
        lines = [
            "# Token Usage",
            "",
            "Token usage reported by completed Codex turns.",
            "",
            "| Turn | Input | Output | Reasoning output | Total |",
            "|---|---:|---:|---:|---:|",
        ]
        for entry in self.token_entries:
            lines.append(
                f"| {_cell(entry.turn)} | {_number(entry.input_tokens)} | {_number(entry.output_tokens)} | "
                f"{_number(entry.reasoning_output_tokens)} | {_number(entry.total_tokens)} |"
            )
        if not self.token_entries:
            lines.append("| no completed turns | - | - | - | - |")
        complete = [entry for entry in self.token_entries if entry.total_tokens is not None]
        lines.extend(
            [
                "",
                "## Total",
                "",
                f"- Input: {sum(entry.input_tokens or 0 for entry in complete)}",
                f"- Output: {sum(entry.output_tokens or 0 for entry in complete)}",
                f"- Reasoning output: {sum(entry.reasoning_output_tokens or 0 for entry in complete)}",
                f"- Total: {sum(entry.total_tokens or 0 for entry in complete)}",
                "",
            ]
        )
        atomic_write_text(self.paths.token_usage, "\n".join(lines))

    def _write_conversation(self) -> None:
        lines = ["# Conversation", ""]
        for index, turn in enumerate(self.conversation_turns, 1):
            lines.extend(
                [
                    f"## {index}. {turn.name}",
                    "",
                    "### User prompt",
                    "",
                    "```text",
                    turn.prompt.rstrip(),
                    "```",
                    "",
                    "### Assistant response",
                    "",
                    "```markdown",
                    turn.response.rstrip(),
                    "```",
                    "",
                ]
            )
            if turn.events:
                lines.extend(["### Codex events", "", "```jsonl", *turn.events, "```", ""])
        if not self.conversation_turns:
            lines.extend(["No completed turns were recorded.", ""])
        atomic_write_text(self.paths.conversation, "\n".join(lines))


@dataclass(frozen=True)
class BoundedText:
    text: str
    raw_bytes: int
    stored_bytes: int
    truncated: bool
    sha256: str


def bounded_text(value: str, max_bytes: int) -> BoundedText:
    raw = value.encode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) <= max_bytes:
        return BoundedText(value, len(raw), len(raw), False, digest)
    stored = raw[:max_bytes]
    text = stored.decode("utf-8", errors="ignore")
    return BoundedText(text, len(raw), len(stored), True, digest)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
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
    if hasattr(value, "__dict__"):
        return {key: _jsonable(child) for key, child in vars(value).items() if not key.startswith("_")}
    return str(value)


def _int_or_none(value: Any, field_name: str) -> int | None:
    field = getattr(value, field_name, None)
    return field if isinstance(field, int) else None


def _item_success(item: Any) -> bool:
    exit_code = getattr(item, "exit_code", None)
    if exit_code is not None:
        return exit_code == 0
    return _status(item).lower() in {"completed", "success", "succeeded", "ok"}


def _status(item: Any) -> str:
    value = getattr(item, "status", "")
    return str(getattr(value, "value", value))


def _cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _inline(value: Any) -> str:
    return " ".join(str(value).split()).replace("`", "\\`")


def _number(value: int | None) -> str:
    return str(value) if value is not None else "-"
