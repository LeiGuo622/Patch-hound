"""Read sanitized commit message and useful diff from Linux git commits."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field

try:
    from dotenv import find_dotenv, load_dotenv
except ModuleNotFoundError:  # pragma: no cover - used by dependency-light MCP subprocesses.
    def find_dotenv(*_args: object, **_kwargs: object) -> str:
        return ""

    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


LINUX_REPO_ENV = "LINUX_REPO_PATH"
COMMIT_HASH_PATTERN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{12,64}(?![0-9a-fA-F])")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TRAILER_KEYS = {
    "acked-by",
    "buglink",
    "cc",
    "closes",
    "co-developed-by",
    "fixes",
    "link",
    "reported-by",
    "reviewed-by",
    "signed-off-by",
    "suggested-by",
    "tested-by",
}
DIFF_METADATA_PREFIXES = (
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "copy from ",
    "copy to ",
    "rename from ",
    "rename to ",
    "similarity index ",
    "dissimilarity index ",
    "Binary files ",
)


def get_commit_diff(commit_id: str) -> str:
    """Return useful diff hunks without noisy git metadata."""

    commit = commit_id.strip()
    if not commit:
        raise ValueError("commit_id must not be empty")

    load_dotenv(find_dotenv())
    repo_path = os.environ.get(LINUX_REPO_ENV)
    if not repo_path:
        raise RuntimeError(f"{LINUX_REPO_ENV} is not configured")

    raw_diff = subprocess.run(
        [
            "git",
            "-C",
            os.path.expanduser(repo_path),
            "show",
            "--format=",
            "--no-ext-diff",
            "--find-renames",
            "--find-copies",
            commit,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    return _format_useful_diff(raw_diff)


@dataclass
class _DiffFile:
    before_path: str | None
    after_path: str | None
    metadata: list[str] = field(default_factory=list)
    hunk_lines: list[str] = field(default_factory=list)
    in_hunk: bool = False

    def format_lines(self) -> list[str]:
        before = self.before_path or "(unknown)"
        after = self.after_path or "(unknown)"
        display_path = self.after_path if self.after_path != "/dev/null" else self.before_path
        if not display_path:
            display_path = after if after != "(unknown)" else before

        return [
            f"File: {display_path}",
            f"Before: {before}",
            f"After: {after}",
            *self.metadata,
            *self.hunk_lines,
        ]


def _format_useful_diff(raw_diff: str) -> str:
    """Return useful diff hunks with normalized before/after file headers."""

    lines: list[str] = []
    current_file: _DiffFile | None = None

    def flush_current_file() -> None:
        nonlocal current_file
        if current_file is None:
            return
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(current_file.format_lines())
        current_file = None

    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            flush_current_file()
            before_path, after_path = _parse_diff_git_paths(line)
            current_file = _DiffFile(before_path=before_path, after_path=after_path)
            continue

        if line.startswith(("diff --cc ", "diff --combined ")):
            flush_current_file()
            _, _, file_path = line.partition(" ")
            _, _, file_path = file_path.partition(" ")
            file_path = _parse_diff_header_path(file_path)
            current_file = _DiffFile(before_path=file_path, after_path=file_path)
            continue

        if current_file is None:
            continue

        if current_file.in_hunk:
            current_file.hunk_lines.append(line)
            continue

        if line.startswith("@@"):
            current_file.hunk_lines.append(line)
            current_file.in_hunk = True
            continue

        if line.startswith("--- "):
            current_file.before_path = _parse_diff_header_path(line.removeprefix("--- "))
            continue

        if line.startswith("+++ "):
            current_file.after_path = _parse_diff_header_path(line.removeprefix("+++ "))
            continue

        if line.startswith(DIFF_METADATA_PREFIXES):
            current_file.metadata.append(line)
            if line.startswith("new file mode "):
                current_file.before_path = "/dev/null"
            elif line.startswith("deleted file mode "):
                current_file.after_path = "/dev/null"

            for prefix in ("rename from ", "copy from "):
                if line.startswith(prefix):
                    current_file.before_path = _parse_metadata_path(line.removeprefix(prefix))
                    break
            for prefix in ("rename to ", "copy to "):
                if line.startswith(prefix):
                    current_file.after_path = _parse_metadata_path(line.removeprefix(prefix))
                    break

            binary_before, binary_after = _parse_binary_files_paths(line)
            if binary_before is not None:
                current_file.before_path = binary_before
            if binary_after is not None:
                current_file.after_path = binary_after

    flush_current_file()
    return "\n".join(lines).strip()


def _parse_diff_git_paths(line: str) -> tuple[str | None, str | None]:
    text = line.removeprefix("diff --git ").strip()
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()

    if len(parts) >= 2:
        return _strip_diff_prefix(parts[0]), _strip_diff_prefix(parts[1])
    if len(parts) == 1:
        file_path = _strip_diff_prefix(parts[0])
        return file_path, file_path
    return None, None


def _parse_diff_header_path(text: str) -> str:
    text = text.strip()
    if text == "/dev/null":
        return text
    if not text:
        return "(unknown)"
    if text.startswith('"'):
        text = _parse_quoted_path(text)
    else:
        text = text.split("	", 1)[0]
    return _strip_diff_prefix(text)


def _parse_metadata_path(text: str) -> str:
    text = text.strip()
    if not text:
        return "(unknown)"
    if text.startswith('"'):
        text = _parse_quoted_path(text)
    return _strip_diff_prefix(text)


def _parse_binary_files_paths(line: str) -> tuple[str | None, str | None]:
    if not line.startswith("Binary files ") or not line.endswith(" differ"):
        return None, None

    text = line.removeprefix("Binary files ").removesuffix(" differ")
    before, separator, after = text.partition(" and ")
    if not separator:
        return None, None
    return _parse_metadata_path(before), _parse_metadata_path(after)


def _parse_quoted_path(text: str) -> str:
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return "(unknown)"
    return parts[0]


def _strip_diff_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def get_commit_message(commit_id: str) -> str:
    """Return commit message text without trailer tags or obvious identifiers."""

    commit = commit_id.strip()
    if not commit:
        raise ValueError("commit_id must not be empty")

    load_dotenv(find_dotenv())
    repo_path = os.environ.get(LINUX_REPO_ENV)
    if not repo_path:
        raise RuntimeError(f"{LINUX_REPO_ENV} is not configured")

    raw_message = subprocess.run(
        [
            "git",
            "-C",
            os.path.expanduser(repo_path),
            "show",
            "-s",
            "--format=%B",
            commit,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    lines = raw_message.splitlines()
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

    message = "\n".join(lines).strip()
    message = COMMIT_HASH_PATTERN.sub("<commit>", message)
    return EMAIL_PATTERN.sub("<email>", message)
