"""Controlled pre-patch history queries for PatchHound cases."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from .commit_info import COMMIT_HASH_PATTERN, EMAIL_PATTERN, TRAILER_KEYS
except ImportError:  # pragma: no cover - used when executed as src/tools/history_context.py.
    from commit_info import COMMIT_HASH_PATTERN, EMAIL_PATTERN, TRAILER_KEYS


def _find_project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("PatchHound project root not found")


PROJECT_ROOT = _find_project_root()
DEFAULT_METADATA_DIR = PROJECT_ROOT / "cases" / "case_metadata"
THIRD_PARTY_LINUX_REPO = PROJECT_ROOT / "third_party" / "linux"
DEFAULT_LINUX_REPO = THIRD_PARTY_LINUX_REPO
DEFAULT_MAX_EVENTS = 5
MAX_EVENTS = 5
DEFAULT_MAX_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
HISTORY_QUERY_LOG_FILENAME = "history_queries.md"
OLD_HISTORY_QUERY_LOG_DESCRIPTION = (
    "This file records sanitized history queries. It does not include commit ids, repository paths, or command output."
)
HISTORY_QUERY_LOG_DESCRIPTION_LINES = (
    "This file records sanitized history queries and returned context.",
    "It does not include commit ids, repository paths, raw git command output, authors, dates, emails, or external links.",
)
CASE_ID_PATTERN = re.compile(r"^case_[0-9]{4,}$")
COMMIT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{12,64}$")
SYMBOL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
URL_PATTERN = re.compile(r"https?://\S+")
QUERY_LOG_ROW_PATTERN = re.compile(r"^\| (?P<sequence>[0-9]+) \|")
INDEX_LINE_PATTERN = re.compile(
    r"^(index )"
    r"[0-9a-fA-F]{7,64}"
    r"\.\."
    r"[0-9a-fA-F]{7,64}"
    r"( .*)?$"
)
EVENT_SEPARATOR = "\x1e"
MESSAGE_END = "\x1f"
SUPPORTED_QUERY_KINDS = frozenset({"function_history", "symbol_history", "file_history"})


@dataclass(frozen=True)
class CaseMetadata:
    case_id: str
    commit_id: str
    source_dir: Path | None


@dataclass(frozen=True)
class PrePatchHistoryQuery:
    kind: str
    file_path: str
    symbol: str | None = None
    max_events: int = DEFAULT_MAX_EVENTS


@dataclass(frozen=True)
class HistoryEvent:
    summary: str
    diff: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class PrePatchHistoryResult:
    query: PrePatchHistoryQuery
    text: str
    event_count: int
    tags: tuple[str, ...]
    truncated: bool


TAG_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("locking", ("lock", "unlock", "mutex", "spinlock", "spin_lock", "rwsem", "rcu")),
    ("lifetime", ("refcount", "kref", "get_", "put_", "hold", "release")),
    ("memory", ("alloc", "free", "kzalloc", "kmalloc", "memcpy", "memset")),
    ("bounds-check", ("len", "size", "count", "nr", "index", "limit", "max", "bound")),
    ("user-input", ("copy_from_user", "copy_to_user", "ioctl", "sysfs", "netlink")),
    ("device-input", ("dma", "mmio", "descriptor", "desc", "ring", "firmware")),
    ("error-path", ("goto err", "goto out", "cleanup", "unwind", "fail", "error")),
    ("permissions", ("capable", "uid", "gid", "permission", "cred", "privilege")),
)

INVARIANT_HINTS: tuple[tuple[str, str], ...] = (
    (
        "bounds-check",
        "Historical changes touched size, count, index, or limit logic; verify the target patch preserves required validation before indexing, copying, or descriptor writes.",
    ),
    (
        "locking",
        "Historical changes touched locking or RCU logic; verify the target patch preserves the required lock context, lock ordering, and IRQ/atomic-context assumptions.",
    ),
    (
        "lifetime",
        "Historical changes touched reference or lifetime logic; verify each successful get/hold/map path still has a matching put/release/unmap path.",
    ),
    (
        "memory",
        "Historical changes touched allocation or memory-copy logic; verify allocations, frees, and copied lengths remain paired and bounded.",
    ),
    (
        "user-input",
        "Historical changes touched user-facing input paths; verify user-controlled data is still validated before use.",
    ),
    (
        "device-input",
        "Historical changes touched device, firmware, DMA, MMIO, descriptor, or ring data; verify device-controlled values are bounded and trusted only after validation.",
    ),
    (
        "error-path",
        "Historical changes touched failure handling; verify newly changed exits preserve cleanup and do not skip required release paths.",
    ),
    (
        "permissions",
        "Historical changes touched credentials or permissions; verify the target patch does not bypass or weaken authorization checks.",
    ),
)


def query_pre_patch_history(
    case_id: str,
    *,
    kind: str,
    file_path: str,
    symbol: str | None = None,
    max_events: int = DEFAULT_MAX_EVENTS,
    linux_repo: Path = DEFAULT_LINUX_REPO,
    metadata_dir: Path = DEFAULT_METADATA_DIR,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    record_query: bool = False,
    query_log_path: Path | None = None,
    query_note: str | None = None,
) -> PrePatchHistoryResult:
    """Return sanitized history from the target commit's first parent and older."""

    metadata = load_case_metadata(case_id, metadata_dir=metadata_dir)
    query = validate_query(
        kind=kind,
        file_path=file_path,
        symbol=symbol,
        max_events=max_events,
    )
    max_bytes = _validate_max_bytes(max_bytes)
    linux_repo = _validate_linux_repo(linux_repo)
    base_rev = f"{metadata.commit_id}^1"

    if not _path_exists_for_query(linux_repo, base_rev, query.file_path, metadata.source_dir):
        raise ValueError(f"file_path is not present in the case source tree or pre-patch tree: {query.file_path}")

    raw_history = _run_history_query(
        linux_repo=linux_repo,
        base_rev=base_rev,
        query=query,
        timeout_seconds=timeout_seconds,
    )
    events = _parse_history_events(raw_history, query.max_events)
    text, truncated = _format_history_result(query, events, max_bytes=max_bytes)
    tags = _dedupe_tags(tag for event in events for tag in event.tags)
    result = PrePatchHistoryResult(
        query=query,
        text=text,
        event_count=len(events),
        tags=tags,
        truncated=truncated,
    )
    if record_query:
        append_history_query_log(metadata, result, path=query_log_path, note=query_note)
    return result


def append_history_query_log(
    metadata: CaseMetadata,
    result: PrePatchHistoryResult,
    *,
    path: Path | None = None,
    note: str | None = None,
) -> Path:
    log_path = path.expanduser().resolve() if path is not None else _default_query_log_path(metadata)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.is_file():
        _upgrade_query_log_header(log_path)
    sequence = _next_query_log_sequence(log_path)
    lines: list[str] = []
    if not log_path.is_file():
        lines.extend(
            [
                "# Pre-Patch History Queries",
                "",
                *HISTORY_QUERY_LOG_DESCRIPTION_LINES,
                "",
                "| # | Case | Kind | File | Symbol | Max Events | Success | Bytes |",
                "|---:|---|---|---|---|---:|---|---:|",
            ]
        )
    lines.append(
        "| "
        f"{sequence} | "
        f"{_markdown_cell(metadata.case_id)} | "
        f"{_markdown_cell(result.query.kind)} | "
        f"{_markdown_cell(result.query.file_path)} | "
        f"{_markdown_cell(result.query.symbol or '-')} | "
        f"{result.query.max_events} | "
        "yes | "
        f"{len(result.text.encode('utf-8'))} |"
    )
    lines.extend(
        [
            "",
            f"## Query {sequence} Result",
            "",
        ]
    )
    sanitized_note = _sanitize_log_note(note)
    if sanitized_note:
        lines.extend(["Query note:", "", sanitized_note, ""])
    lines.extend(["Returned context:", "", "````markdown", result.text.rstrip(), "````", ""])
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("\n".join(lines) + "\n")
    return log_path


def _upgrade_query_log_header(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if OLD_HISTORY_QUERY_LOG_DESCRIPTION not in text:
        return
    replacement = "\n".join(HISTORY_QUERY_LOG_DESCRIPTION_LINES)
    path.write_text(text.replace(OLD_HISTORY_QUERY_LOG_DESCRIPTION, replacement, 1), encoding="utf-8")


def load_case_metadata(case_id: str, *, metadata_dir: Path = DEFAULT_METADATA_DIR) -> CaseMetadata:
    case_id = case_id.strip()
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError(f"case_id must look like case_0001, got: {case_id}")

    path = metadata_dir.expanduser().resolve() / f"{case_id}.json"
    if not path.is_file():
        raise FileNotFoundError(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata_case_id = str(payload.get("case_id", "")).strip()
    if metadata_case_id and metadata_case_id != case_id:
        raise ValueError(f"metadata case_id mismatch: expected {case_id}, got {metadata_case_id}")

    commit_id = str(payload.get("commit_id", "")).strip()
    if not COMMIT_ID_PATTERN.fullmatch(commit_id):
        raise ValueError(f"metadata commit_id must be a hexadecimal commit id: {path}")

    source_dir = payload.get("source_dir")
    source_path: Path | None = None
    if isinstance(source_dir, str) and source_dir.strip():
        raw_source_path = Path(source_dir).expanduser()
        source_path = raw_source_path if raw_source_path.is_absolute() else PROJECT_ROOT / raw_source_path

    return CaseMetadata(case_id=case_id, commit_id=commit_id, source_dir=source_path)


def validate_query(
    *,
    kind: str,
    file_path: str,
    symbol: str | None = None,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> PrePatchHistoryQuery:
    normalized_kind = kind.strip()
    if normalized_kind not in SUPPORTED_QUERY_KINDS:
        supported = ", ".join(sorted(SUPPORTED_QUERY_KINDS))
        raise ValueError(f"unsupported history query kind: {kind}; supported: {supported}")

    normalized_path = normalize_repo_path(file_path)
    normalized_symbol = _normalize_symbol(symbol)
    if normalized_kind in {"function_history", "symbol_history"} and normalized_symbol is None:
        raise ValueError(f"{normalized_kind} requires symbol")
    if normalized_kind == "file_history" and normalized_symbol is not None:
        raise ValueError("file_history does not accept symbol")

    return PrePatchHistoryQuery(
        kind=normalized_kind,
        file_path=normalized_path,
        symbol=normalized_symbol,
        max_events=_validate_max_events(max_events),
    )


def normalize_repo_path(file_path: str) -> str:
    value = file_path.strip().replace("\\", "/")
    if not value:
        raise ValueError("file_path must not be empty")
    if "\x00" in value:
        raise ValueError("file_path must not contain NUL bytes")
    if value.startswith("/"):
        raise ValueError("file_path must be repository-relative")

    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("file_path must be normalized and must not contain . or ..")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError("file_path must be repository-relative")
    if normalized != value:
        raise ValueError("file_path must already be normalized")
    return normalized


def sanitize_history_text(text: str) -> str:
    """Remove identity, revision, and external-link metadata from git output."""

    sanitized_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _is_metadata_line(stripped):
            continue
        key, separator, value = stripped.partition(":")
        if separator and value.strip() and key.lower() in TRAILER_KEYS:
            continue
        if INDEX_LINE_PATTERN.match(line):
            line = "index <blob>..<blob>"
        line = URL_PATTERN.sub("<url>", line)
        line = COMMIT_HASH_PATTERN.sub("<commit>", line)
        line = EMAIL_PATTERN.sub("<email>", line)
        sanitized_lines.append(line.rstrip())
    return "\n".join(sanitized_lines).strip()


def _run_history_query(
    *,
    linux_repo: Path,
    base_rev: str,
    query: PrePatchHistoryQuery,
    timeout_seconds: int,
) -> str:
    command = _build_history_command(linux_repo=linux_repo, base_rev=base_rev, query=query)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"pre-patch history query timed out after {timeout_seconds} seconds") from exc

    if completed.returncode != 0:
        sanitized_stderr = sanitize_history_text(completed.stderr)
        if _looks_like_no_history(sanitized_stderr):
            return ""
        raise RuntimeError(f"pre-patch history query failed: {_one_line(sanitized_stderr, 240)}")
    return completed.stdout


def _build_history_command(*, linux_repo: Path, base_rev: str, query: PrePatchHistoryQuery) -> list[str]:
    base_command = [
        "git",
        "-C",
        str(linux_repo),
        "log",
        "--no-color",
        "--no-ext-diff",
        "--find-renames",
        f"--max-count={query.max_events}",
        f"--format={EVENT_SEPARATOR}%n%B{MESSAGE_END}",
    ]

    if query.kind == "function_history":
        assert query.symbol is not None
        return [
            *base_command,
            "-L",
            f":{query.symbol}:{query.file_path}",
            base_rev,
        ]
    if query.kind == "symbol_history":
        assert query.symbol is not None
        return [
            *base_command,
            "-G",
            _symbol_diff_regex(query.symbol),
            "-p",
            base_rev,
            "--",
            query.file_path,
        ]
    if query.kind == "file_history":
        return [
            *base_command,
            "-p",
            "--follow",
            base_rev,
            "--",
            query.file_path,
        ]
    raise AssertionError(f"unsupported query kind after validation: {query.kind}")


def _parse_history_events(raw_history: str, max_events: int) -> list[HistoryEvent]:
    events: list[HistoryEvent] = []
    for chunk in raw_history.split(EVENT_SEPARATOR):
        chunk = chunk.strip()
        if not chunk:
            continue
        message, separator, diff = chunk.partition(MESSAGE_END)
        if not separator:
            message = ""
            diff = chunk
        sanitized_message = sanitize_history_text(_strip_trailing_trailers(message))
        sanitized_diff = sanitize_history_text(diff)
        event_text = "\n".join(part for part in (sanitized_message, sanitized_diff) if part).strip()
        if not event_text:
            continue
        events.append(
            HistoryEvent(
                summary=_summary_from_message(sanitized_message),
                diff=sanitized_diff,
                tags=_infer_tags(event_text),
            )
        )
        if len(events) >= max_events:
            break
    return events


def _format_history_result(
    query: PrePatchHistoryQuery,
    events: list[HistoryEvent],
    *,
    max_bytes: int,
) -> tuple[str, bool]:
    lines = [
        "# Pre-Patch History Query",
        "",
        "Scope:",
        "- Only target patch ancestors were searched.",
        "- Commit identifiers, authors, dates, emails, and external links are redacted.",
        "",
        "Query:",
        f"- kind: {query.kind}",
        f"- file: {query.file_path}",
    ]
    if query.symbol is not None:
        lines.append(f"- symbol: {query.symbol}")
    lines.extend([f"- max_events: {query.max_events}", ""])

    if not events:
        lines.extend(
            [
                "No matching pre-patch history was found for this query.",
                "",
                "Invariant candidates:",
                "- None inferred from pre-patch history.",
            ]
        )
        return _cap_text("\n".join(lines).rstrip() + "\n", max_bytes)

    for index, event in enumerate(events, start=1):
        lines.extend(
            [
                f"## Event {index}",
                "",
                "Summary:",
                event.summary or "(no subject after sanitization)",
                "",
                "Tags:",
                ", ".join(event.tags) if event.tags else "none",
                "",
                "Relevant diff:",
                "```diff",
                event.diff or "(no diff returned)",
                "```",
                "",
            ]
        )

    tags = _dedupe_tags(tag for event in events for tag in event.tags)
    lines.append("Invariant candidates:")
    hints = [hint for tag, hint in INVARIANT_HINTS if tag in tags]
    if hints:
        lines.extend(f"- {hint}" for hint in hints)
    else:
        lines.append("- None inferred from lightweight tags.")

    return _cap_text("\n".join(lines).rstrip() + "\n", max_bytes)


def _cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False

    suffix = "\n\n[Output truncated by PatchHound history tool]\n"
    suffix_bytes = suffix.encode("utf-8")
    cap = max(0, max_bytes - len(suffix_bytes))
    clipped = encoded[:cap].decode("utf-8", errors="ignore").rstrip()
    return clipped + suffix, True


def _strip_trailing_trailers(message: str) -> str:
    lines = message.splitlines()
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1

    start = end
    while start > 0:
        key, separator, value = lines[start - 1].partition(":")
        if not (separator and value.strip() and key.lower() in TRAILER_KEYS):
            break
        start -= 1

    if start < end:
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        lines = lines[:start]
    else:
        lines = lines[:end]
    return "\n".join(lines).strip()


def _summary_from_message(message: str) -> str:
    for line in message.splitlines():
        if line.strip():
            return line.strip()
    return "(no subject after sanitization)"


def _infer_tags(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    tags: list[str] = []
    for tag, keywords in TAG_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            tags.append(tag)
    return tuple(tags)


def _dedupe_tags(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        tag = str(value)
        if tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return tuple(deduped)


def _normalize_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    value = symbol.strip()
    if not value:
        return None
    if not SYMBOL_PATTERN.fullmatch(value):
        raise ValueError("symbol must be a simple C identifier")
    return value


def _symbol_diff_regex(symbol: str) -> str:
    escaped = re.escape(symbol)
    return rf"(^|[^A-Za-z0-9_]){escaped}([^A-Za-z0-9_]|$)"


def _validate_max_events(value: int) -> int:
    if not isinstance(value, int):
        raise ValueError("max_events must be an integer")
    if value < 1:
        raise ValueError("max_events must be at least 1")
    return min(value, MAX_EVENTS)


def _validate_max_bytes(value: int) -> int:
    if not isinstance(value, int):
        raise ValueError("max_bytes must be an integer")
    if value < 1024:
        raise ValueError("max_bytes must be at least 1024")
    return min(value, MAX_OUTPUT_BYTES)


def _validate_linux_repo(path: Path) -> Path:
    repo = path.expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(repo)
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        raise RuntimeError(f"Linux repository is not a git work tree: {repo}")
    return repo


def _path_exists_for_query(
    linux_repo: Path,
    base_rev: str,
    file_path: str,
    source_dir: Path | None,
) -> bool:
    if source_dir is not None and (source_dir / file_path).exists():
        return True
    completed = subprocess.run(
        ["git", "-C", str(linux_repo), "cat-file", "-e", f"{base_rev}:{file_path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _default_query_log_path(metadata: CaseMetadata) -> Path:
    if metadata.source_dir is not None and metadata.source_dir.name == "source":
        return metadata.source_dir.parent / "output" / HISTORY_QUERY_LOG_FILENAME
    return PROJECT_ROOT / "cases" / metadata.case_id / "output" / HISTORY_QUERY_LOG_FILENAME


def _next_query_log_sequence(path: Path) -> int:
    if not path.is_file():
        return 1
    highest = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = QUERY_LOG_ROW_PATTERN.match(stripped)
        if not match:
            continue
        highest = max(highest, int(match.group("sequence")))
    return highest + 1


def _sanitize_log_note(note: str | None) -> str | None:
    if note is None:
        return None
    text = sanitize_history_text(str(note)).strip()
    return _one_line(text, 500) if text else None


def _markdown_cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def _is_metadata_line(stripped: str) -> bool:
    lowered = stripped.lower()
    if lowered.startswith(("commit ", "author:", "author ", "committer:", "committer ", "date:", "merge:")):
        return True
    return False


def _looks_like_no_history(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        phrase in lowered
        for phrase in (
            "there is no path",
            "no match",
            "not found",
            "does not exist",
        )
    )


def _one_line(value: str, max_len: int) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 3] + "..."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id", help="Prepared PatchHound case id, for example case_0001")
    parser.add_argument(
        "--kind",
        required=True,
        choices=sorted(SUPPORTED_QUERY_KINDS),
        help="Pre-patch history query kind",
    )
    parser.add_argument(
        "--file",
        dest="file_path",
        required=True,
        help="Repository-relative path visible in the case source tree",
    )
    parser.add_argument("--symbol", default=None, help="Function or symbol name")
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--query-log",
        type=Path,
        default=None,
        help=f"Query log path; defaults to case output/{HISTORY_QUERY_LOG_FILENAME}",
    )
    parser.add_argument(
        "--no-query-log",
        action="store_true",
        help="Do not append this CLI query to the case history query log",
    )
    parser.add_argument(
        "--linux-repo",
        type=Path,
        default=DEFAULT_LINUX_REPO,
        help="Host-side Linux git repository; hidden from analysis agents",
    )
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=DEFAULT_METADATA_DIR,
        help="PatchHound case metadata directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = query_pre_patch_history(
            args.case_id,
            kind=args.kind,
            file_path=args.file_path,
            symbol=args.symbol,
            max_events=args.max_events,
            linux_repo=args.linux_repo,
            metadata_dir=args.metadata_dir,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout_seconds,
            record_query=not args.no_query_log,
            query_log_path=args.query_log,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should return concise validation/runtime errors.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(result.text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
