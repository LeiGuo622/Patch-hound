"""Prepared Case validation and expert-run directory allocation."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

CASE_ID_PATTERN = re.compile(r"^case_[0-9]{4,}$")
RUN_ID_PATTERN = re.compile(r"^run_[0-9]{4,}$")
EXPERT_RUNS_DIR_NAME = "expert-runs"


@dataclass(frozen=True)
class CaseData:
    case_id: str
    case_dir: Path
    source_dir: Path
    commit_message: str
    diff: str


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    run_dir: Path
    analysis: Path
    manifest: Path
    model_config: Path
    process_log: Path
    token_usage: Path
    conversation: Path
    history_queries: Path


def load_case(case_id: str, cases_dir: Path) -> CaseData:
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError(f"case_id must look like case_0001, got: {case_id}")
    case_dir = (cases_dir / case_id).expanduser().resolve()
    input_dir = case_dir / "input"
    source_dir = case_dir / "source"
    commit_message_path = input_dir / "commit_message.txt"
    diff_path = input_dir / "diff.txt"
    for path in (case_dir, source_dir):
        if not path.is_dir():
            raise FileNotFoundError(path)
    for path in (commit_message_path, diff_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if (case_dir / ".git").exists() or (source_dir / ".git").exists():
        raise RuntimeError(f"case directory must not contain a .git directory: {case_dir}")
    return CaseData(
        case_id=case_id,
        case_dir=case_dir,
        source_dir=source_dir,
        commit_message=commit_message_path.read_text(encoding="utf-8"),
        diff=diff_path.read_text(encoding="utf-8"),
    )


def format_run_id(number: int) -> str:
    if number < 1:
        raise ValueError("run number must be at least 1")
    return f"run_{number:04d}"


def next_run_number(case_dir: Path) -> int:
    runs_dir = case_dir / EXPERT_RUNS_DIR_NAME
    if not runs_dir.is_dir():
        return 1
    numbers = [
        int(child.name.rsplit("_", 1)[1])
        for child in runs_dir.iterdir()
        if RUN_ID_PATTERN.fullmatch(child.name)
    ]
    return max(numbers, default=0) + 1


def allocate_run_paths(
    case_dir: Path,
    *,
    run_id: str | None = None,
    force_run: bool = False,
    output_root: Path | None = None,
) -> RunPaths:
    if force_run and run_id is None:
        raise ValueError("--force-run requires --run-id")
    runs_dir = (
        output_root.expanduser().resolve()
        if output_root is not None
        else case_dir / EXPERT_RUNS_DIR_NAME
    )
    runs_dir.mkdir(parents=True, exist_ok=True)
    if run_id is not None:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(f"run_id must look like run_0001, got: {run_id}")
        run_dir = runs_dir / run_id
        if run_dir.exists():
            if not force_run:
                raise RuntimeError(f"Run directory already exists; use --force-run to replace it: {run_dir}")
            shutil.rmtree(run_dir)
        run_dir.mkdir()
        return _run_paths(run_dir)
    number = next_run_number(case_dir)
    while True:
        run_dir = runs_dir / format_run_id(number)
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            number += 1
            continue
        return _run_paths(run_dir)


def _run_paths(run_dir: Path) -> RunPaths:
    return RunPaths(
        run_id=run_dir.name,
        run_dir=run_dir,
        analysis=run_dir / "analysis.md",
        manifest=run_dir / "run_manifest.json",
        model_config=run_dir / "model_config.md",
        process_log=run_dir / "process_log.md",
        token_usage=run_dir / "token_usage.md",
        conversation=run_dir / "conversation.md",
        history_queries=run_dir / "history_queries.md",
    )
