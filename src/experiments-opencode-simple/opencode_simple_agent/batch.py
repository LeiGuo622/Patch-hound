"""Sequential batch orchestration, matching experiments-codex-simple CLI semantics."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .case_io import CASE_ID_PATTERN
from .config import DEFAULT_CASES_DIR, PROJECT_ROOT

RUN_CASE_SCRIPT = Path("src/experiments-opencode-simple/run_case.py")


@dataclass(frozen=True)
class BatchResult:
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def parse_case_id(value: str) -> int:
    if not CASE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"expected case ID like case_0001, got: {value}")
    number = int(value.rsplit("_", 1)[1])
    if number < 1:
        raise ValueError("case number must be at least 1")
    return number


def format_case_id(number: int) -> str:
    if number < 1:
        raise ValueError("case number must be at least 1")
    return f"case_{number:04d}"


def discover_case_numbers(cases_dir: Path = DEFAULT_CASES_DIR) -> list[int]:
    root = cases_dir.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    return sorted(
        int(path.name.rsplit("_", 1)[1])
        for path in root.iterdir()
        if path.is_dir() and CASE_ID_PATTERN.fullmatch(path.name)
    )


def select_case_numbers(
    numbers: list[int], *, start: int | None, after: int | None, end: int | None
) -> list[int]:
    if start is not None and after is not None:
        raise ValueError("--start and --after are mutually exclusive")
    lower = start if start is not None else (after + 1 if after is not None else 1)
    upper = end if end is not None else max(numbers, default=0)
    return [] if upper < lower else [number for number in numbers if lower <= number <= upper]


def active_case_ids() -> set[str]:
    result = subprocess.run(["ps", "-eo", "args="], check=True, capture_output=True, text=True)
    active: set[str] = set()
    for line in result.stdout.splitlines():
        if str(RUN_CASE_SCRIPT) in line:
            active.update(match.group(0) for match in re.finditer(r"\bcase_[0-9]{4,}\b", line))
    return active


def build_command(case_id: str, args: list[str]) -> list[str]:
    return ["uv", "run", "--project", "src/experiments-opencode-simple", "python", str(RUN_CASE_SCRIPT), case_id, *args]


def run_batch(
    *,
    cases_dir: Path,
    start: int | None,
    after: int | None,
    end: int | None,
    continue_on_error: bool,
    wait_active: bool,
    poll_seconds: float,
    dry_run: bool,
    run_case_args: list[str],
) -> BatchResult:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    selected = select_case_numbers(discover_case_numbers(cases_dir), start=start, after=after, end=end)
    succeeded: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for number in selected:
        case_id = format_case_id(number)
        active = active_case_ids()
        if case_id in active and wait_active:
            while case_id in active:
                print(f"[patchhound-opencode-simple-batch] waiting for active case {case_id}")
                time.sleep(poll_seconds)
                active = active_case_ids()
        elif case_id in active:
            print(f"[patchhound-opencode-simple-batch] skipping active case {case_id}")
            skipped.append(case_id)
            continue
        command = build_command(case_id, run_case_args)
        print(f"[patchhound-opencode-simple-batch] running: {' '.join(command)}")
        if dry_run:
            succeeded.append(case_id)
            continue
        started_at = time.monotonic()
        result = subprocess.run(command, cwd=PROJECT_ROOT)
        duration = time.monotonic() - started_at
        if result.returncode == 0:
            succeeded.append(case_id)
            print(f"[patchhound-opencode-simple-batch] {case_id} completed in {duration:.1f}s")
            continue
        failed.append(case_id)
        print(f"[patchhound-opencode-simple-batch] {case_id} failed in {duration:.1f}s with exit {result.returncode}")
        if not continue_on_error:
            break
    return BatchResult(tuple(succeeded), tuple(failed), tuple(skipped))

