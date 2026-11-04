"""Host-owned, atomic and bounded run artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .case_io import RunPaths


@dataclass(frozen=True)
class ProcessEntry:
    timestamp: str
    component: str
    action: str
    success: bool
    duration_ms: int | None
    detail: str


@dataclass(frozen=True)
class TokenEntry:
    turn: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    total_tokens: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ConversationTurn:
    name: str
    prompt: str
    response: str
    events: tuple[dict[str, Any], ...]


@dataclass
class RunArtifacts:
    paths: RunPaths
    save_conversation: bool = False
    process_entries: list[ProcessEntry] = field(default_factory=list)
    token_entries: list[TokenEntry] = field(default_factory=list)
    conversation: list[ConversationTurn] = field(default_factory=list)
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
        *,
        duration_ms: int | None = None,
    ) -> None:
        self.process_entries.append(
            ProcessEntry(
                timestamp=utc_now(),
                component=component,
                action=action,
                success=success,
                duration_ms=duration_ms,
                detail=" ".join(str(detail).split())[:4000],
            )
        )

    def record_tokens(self, turn: str, usage: dict[str, Any]) -> None:
        self.token_entries.append(
            TokenEntry(
                turn=turn,
                input_tokens=_int_or_none(usage.get("input")),
                output_tokens=_int_or_none(usage.get("output")),
                reasoning_tokens=_int_or_none(usage.get("reasoning")),
                cache_read_tokens=_int_or_none(usage.get("cache_read")),
                cache_write_tokens=_int_or_none(usage.get("cache_write")),
                total_tokens=_int_or_none(usage.get("total")),
                raw=dict(usage.get("raw")) if isinstance(usage.get("raw"), dict) else {},
            )
        )

    def record_conversation(
        self,
        name: str,
        prompt: str,
        response: str,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        if self.save_conversation:
            self.conversation.append(ConversationTurn(name, prompt, response, events))

    def write_manifest(self) -> None:
        atomic_write_text(
            self.paths.manifest,
            json.dumps(self.manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        )

    def write_model_config(self, values: dict[str, Any]) -> None:
        lines = ["# Model Configuration", ""]
        lines.extend(f"- {label}: `{_inline(value)}`" for label, value in values.items())
        atomic_write_text(self.paths.model_config, "\n".join(lines) + "\n")

    def write_analysis(self, text: str) -> None:
        atomic_write_text(self.paths.analysis, text.rstrip() + "\n")

    def write_invalid_response(self, text: str) -> None:
        atomic_write_text(self.paths.invalid_response, text.rstrip() + "\n")

    def collect_history_log(self, scratch_log: Path) -> None:
        if scratch_log.is_file():
            temporary = self.paths.history_queries.with_name(f".{self.paths.history_queries.name}.{uuid4().hex}.tmp")
            shutil.copy2(scratch_log, temporary)
            temporary.replace(self.paths.history_queries)

    def ensure_history_log(self, *, enabled: bool) -> None:
        if self.paths.history_queries.is_file():
            return
        state = "No persisted history queries were recorded for this run." if enabled else "History tool disabled."
        atomic_write_text(self.paths.history_queries, f"# Pre-Patch History Queries\n\n{state}\n")

    def write_logs(self) -> None:
        self._write_process_log()
        self._write_token_usage()
        if self.save_conversation:
            self._write_conversation()

    def _write_process_log(self) -> None:
        lines = [
            "# Process Log",
            "",
            "Host-recorded OMP runtime, session, turn, tool, validation, isolation, and artifact events.",
            "",
            "| # | Time (UTC) | Component | Action | Success | Duration ms | Detail |",
            "|---:|---|---|---|---|---:|---|",
        ]
        if not self.process_entries:
            lines.append("| 1 | - | runner | no entries | yes | - | - |")
        for index, entry in enumerate(self.process_entries, 1):
            lines.append(
                f"| {index} | {_cell(entry.timestamp)} | {_cell(entry.component)} | {_cell(entry.action)} | "
                f"{'yes' if entry.success else 'no'} | {_number(entry.duration_ms)} | {_cell(entry.detail or '-')} |"
            )
        atomic_write_text(self.paths.process_log, "\n".join(lines) + "\n")

    def _write_token_usage(self) -> None:
        lines = [
            "# Token Usage",
            "",
            "Token usage reported by completed OMP turns. Missing SDK fields are shown as `null`.",
            "",
            "| Turn | Input | Output | Reasoning | Cache read | Cache write | Total |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for entry in self.token_entries:
            lines.append(
                f"| {_cell(entry.turn)} | {_number(entry.input_tokens)} | {_number(entry.output_tokens)} | "
                f"{_number(entry.reasoning_tokens)} | {_number(entry.cache_read_tokens)} | "
                f"{_number(entry.cache_write_tokens)} | {_number(entry.total_tokens)} |"
            )
        if not self.token_entries:
            lines.append("| no completed turns | null | null | null | null | null | null |")
        lines.extend(["", "## Total", ""])
        for label, attribute in (
            ("Input", "input_tokens"),
            ("Output", "output_tokens"),
            ("Reasoning", "reasoning_tokens"),
            ("Cache read", "cache_read_tokens"),
            ("Cache write", "cache_write_tokens"),
            ("Total", "total_tokens"),
        ):
            values = [getattr(entry, attribute) for entry in self.token_entries]
            total = sum(value for value in values if value is not None) if any(value is not None for value in values) else None
            lines.append(f"- {label}: {_number(total)}")
        lines.append("")
        atomic_write_text(self.paths.token_usage, "\n".join(lines))

    def _write_conversation(self) -> None:
        lines = ["# Conversation", ""]
        for index, turn in enumerate(self.conversation, 1):
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
                    "### Normalized events",
                    "",
                    "```jsonl",
                ]
            )
            lines.extend(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in turn.events)
            lines.extend(["```", ""])
        if not self.conversation:
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
    return BoundedText(stored.decode("utf-8", errors="ignore"), len(raw), len(stored), True, digest)


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
        temporary.unlink(missing_ok=True)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _inline(value: Any) -> str:
    if value is None:
        return "null"
    return " ".join(str(value).split()).replace("`", "\\`")


def _number(value: int | None) -> str:
    return str(value) if value is not None else "null"

