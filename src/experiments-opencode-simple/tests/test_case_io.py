from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from opencode_simple_agent.case_io import allocate_run_paths, load_case


def test_loads_current_prepared_case_contract(prepared_cases: Path) -> None:
    case = load_case("case_0001", prepared_cases)
    assert case.commit_message == "修复 carrier race\n"
    assert "drivers/test.c" in case.diff
    assert case.source_dir == (prepared_cases / "case_0001" / "source").resolve()


def test_missing_input_is_rejected(prepared_cases: Path) -> None:
    (prepared_cases / "case_0001" / "input" / "diff.txt").unlink()
    with pytest.raises(FileNotFoundError):
        load_case("case_0001", prepared_cases)


def test_source_symlink_escape_is_rejected(prepared_cases: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (prepared_cases / "case_0001" / "source" / "escape").symlink_to(outside)
    with pytest.raises(RuntimeError, match="escapes"):
        load_case("case_0001", prepared_cases)


def test_run_allocation_is_atomic(prepared_cases: Path, tmp_path: Path) -> None:
    case = load_case("case_0001", prepared_cases)
    output = tmp_path / "runs"
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _index: allocate_run_paths(case.case_dir, output_root=output), range(16)))
    assert len({path.run_id for path in paths}) == 16
    assert all(path.run_dir.is_dir() for path in paths)


def test_force_requires_explicit_run_id(prepared_cases: Path) -> None:
    case = load_case("case_0001", prepared_cases)
    with pytest.raises(ValueError, match="requires"):
        allocate_run_paths(case.case_dir, force_run=True)
