from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from expert_agent.batch import build_command, discover_case_numbers, run_batch, select_case_numbers  # noqa: E402


class BatchTest(unittest.TestCase):
    def test_discovery_selection_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("case_0003", "case_0001", "not-a-case"):
                (root / name).mkdir()
            self.assertEqual(discover_case_numbers(root), [1, 3])
        self.assertEqual(select_case_numbers([1, 2, 3], start=None, after=1, end=3), [2, 3])
        self.assertIn("src/experiments-codex-expert/run_case.py", build_command("case_0001", ["--quiet"]))

    def test_batch_is_sequential_and_forwards_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("case_0001", "case_0002"):
                (root / name).mkdir()
            with (
                patch("expert_agent.batch.active_case_ids", return_value=set()),
                patch("expert_agent.batch.subprocess.run") as run,
            ):
                run.return_value.returncode = 0
                result = run_batch(
                    cases_dir=root,
                    start=None,
                    after=None,
                    end=None,
                    continue_on_error=False,
                    wait_active=False,
                    poll_seconds=1,
                    dry_run=False,
                    run_case_args=["--quiet"],
                )
            self.assertEqual(result.succeeded, ("case_0001", "case_0002"))
            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
