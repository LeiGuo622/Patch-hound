from __future__ import annotations

from pathlib import Path

import pytest

from opencode_simple_agent import batch


def make_cases(root: Path, numbers: tuple[int, ...]) -> None:
    root.mkdir()
    for number in numbers:
        (root / f"case_{number:04d}").mkdir()


def test_case_range_selection_and_formatting(tmp_path: Path) -> None:
    make_cases(tmp_path / "cases", (1, 2, 4, 10))
    numbers = batch.discover_case_numbers(tmp_path / "cases")
    assert batch.select_case_numbers(numbers, start=2, after=None, end=4) == [2, 4]
    assert batch.select_case_numbers(numbers, start=None, after=2, end=None) == [4, 10]
    assert batch.parse_case_id("case_0010") == 10
    assert batch.format_case_id(10) == "case_0010"
    with pytest.raises(ValueError, match="mutually exclusive"):
        batch.select_case_numbers(numbers, start=1, after=1, end=None)


def test_batch_stops_or_continues_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = tmp_path / "cases"
    make_cases(cases, (1, 2, 3))
    monkeypatch.setattr(batch, "active_case_ids", lambda: set())

    class Result:
        def __init__(self, code: int) -> None:
            self.returncode = code

    codes = iter((0, 7, 0))
    monkeypatch.setattr(batch.subprocess, "run", lambda *_args, **_kwargs: Result(next(codes)))
    stopped = batch.run_batch(
        cases_dir=cases,
        start=None,
        after=None,
        end=None,
        continue_on_error=False,
        wait_active=False,
        poll_seconds=0.01,
        dry_run=False,
        run_case_args=[],
    )
    assert stopped.succeeded == ("case_0001",)
    assert stopped.failed == ("case_0002",)
    assert stopped.exit_code == 1

    codes = iter((0, 7, 0))
    continued = batch.run_batch(
        cases_dir=cases,
        start=None,
        after=None,
        end=None,
        continue_on_error=True,
        wait_active=False,
        poll_seconds=0.01,
        dry_run=False,
        run_case_args=["--quiet"],
    )
    assert continued.succeeded == ("case_0001", "case_0003")
    assert continued.failed == ("case_0002",)
