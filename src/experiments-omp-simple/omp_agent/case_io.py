"""Prepared Case validation and omp-run allocation."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

CASE_ID_PATTERN = re.compile(r"^case_[0-9]{4,}$")
RUN_ID_PATTERN = re.compile(r"^run_[0-9]{4,}$")


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
    invalid_response: Path


def load_case(case_id: str, cases_dir: Path) -> CaseData:
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError(f"case_id must look like case_0001, got: {case_id}")
    cases_root = cases_dir.expanduser().resolve()
    lexical_case = cases_root / case_id
    if lexical_case.is_symlink():
        raise RuntimeError(f"case directory must not be a symlink: {lexical_case}")
    case_dir = lexical_case.resolve()
    if not case_dir.is_relative_to(cases_root):
        raise RuntimeError(f"case directory escapes cases root: {case_dir}")
    source_dir = case_dir / "source"
    commit_path = case_dir / "input" / "commit_message.txt"
    diff_path = case_dir / "input" / "diff.txt"
    if not case_dir.is_dir():
        raise FileNotFoundError(case_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(source_dir)
    if source_dir.is_symlink():
        raise RuntimeError(f"source directory must not be a symlink: {source_dir}")
    _require_safe_file(commit_path, case_dir)
    _require_safe_file(diff_path, case_dir)
    if (case_dir / ".git").exists() or (source_dir / ".git").exists():
        raise RuntimeError(f"case directory must not contain .git: {case_dir}")
    _reject_escaping_symlinks(source_dir)
    return CaseData(
        case_id=case_id,
        case_dir=case_dir,
        source_dir=source_dir,
        commit_message=commit_path.read_text(encoding="utf-8"),
        diff=diff_path.read_text(encoding="utf-8"),
    )


def allocate_run_paths(
    case_dir: Path,
    *,
    output_dir_name: str = "omp-runs",
    output_root: Path | None = None,
    run_id: str | None = None,
    force_run: bool = False,
) -> RunPaths:
    if force_run and run_id is None:
        raise ValueError("--force-run requires --run-id")
    runs_dir = (output_root.expanduser().resolve() if output_root else case_dir / output_dir_name)
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
        return _paths(run_dir)
    number = _next_run_number(runs_dir)
    while True:
        run_dir = runs_dir / f"run_{number:04d}"
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            number += 1
            continue
        return _paths(run_dir)


def _next_run_number(runs_dir: Path) -> int:
    highest = 0
    for child in runs_dir.iterdir():
        if RUN_ID_PATTERN.fullmatch(child.name):
            highest = max(highest, int(child.name.rsplit("_", 1)[1]))
    return highest + 1


def _paths(run_dir: Path) -> RunPaths:
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
        invalid_response=run_dir / "invalid_response.md",
    )


def _require_safe_file(path: Path, root: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    if not path.resolve().is_relative_to(root):
        raise RuntimeError(f"case input escapes case directory: {path}")


def _reject_escaping_symlinks(source_dir: Path) -> None:
    root = source_dir.resolve()
    for current, directories, files in os.walk(source_dir, followlinks=False):
        base = Path(current)
        for name in (*directories, *files):
            candidate = base / name
            if candidate.is_symlink() and not candidate.resolve(strict=False).is_relative_to(root):
                raise RuntimeError(f"source symlink escapes source tree: {candidate}")


