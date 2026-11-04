from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from simple_agent.batch import (  # noqa: E402
    build_command,
    discover_case_numbers,
    run_batch,
    select_case_numbers,
)


class BatchTest(unittest.TestCase):
    def test_discovers_and_selects_case_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("case_0003", "case_0001", "case_metadata", "notes"):
                (root / name).mkdir()
            numbers = discover_case_numbers(root)

        self.assertEqual(numbers, [1, 3])
        self.assertEqual(select_case_numbers(numbers, start=2, after=None, end=None), [3])
        self.assertEqual(select_case_numbers(numbers, start=None, after=1, end=3), [3])

    def test_build_command_does_not_force_conversation(self) -> None:
        self.assertEqual(
            build_command("case_0007", ["--quiet"]),
            ["uv", "run", "python", "src/experiments-codex-simple/run_case.py", "case_0007", "--quiet"],
        )

    def test_continue_on_error_and_skip_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("case_0001", "case_0002", "case_0003"):
                (root / name).mkdir()
            completed = iter([SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)])
            with (
                patch("simple_agent.batch.active_case_ids", return_value={"case_0002"}),
                patch("simple_agent.batch.subprocess.run", side_effect=lambda *_args, **_kwargs: next(completed)),
            ):
                result = run_batch(
                    cases_dir=root,
                    start=1,
                    after=None,
                    end=3,
                    continue_on_error=True,
                    wait_active=False,
                    poll_seconds=1,
                    dry_run=False,
                    run_case_args=["--quiet"],
                )

        self.assertEqual(result.failed, ("case_0001",))
        self.assertEqual(result.skipped, ("case_0002",))
        self.assertEqual(result.succeeded, ("case_0003",))
        self.assertEqual(result.exit_code, 1)

    def test_default_stops_after_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("case_0001", "case_0002"):
                (root / name).mkdir()
            with (
                patch("simple_agent.batch.active_case_ids", return_value=set()),
                patch(
                    "simple_agent.batch.subprocess.run",
                    return_value=SimpleNamespace(returncode=1),
                ) as run,
            ):
                result = run_batch(
                    cases_dir=root,
                    start=None,
                    after=None,
                    end=None,
                    continue_on_error=False,
                    wait_active=False,
                    poll_seconds=1,
                    dry_run=False,
                    run_case_args=[],
                )

        self.assertEqual(result.failed, ("case_0001",))
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
