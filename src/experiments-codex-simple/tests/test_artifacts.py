from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from simple_agent.artifacts import RunArtifacts  # noqa: E402
from simple_agent.case_io import allocate_run_paths  # noqa: E402


class ArtifactsTest(unittest.TestCase):
    def test_zero_query_log_is_created_and_real_log_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = allocate_run_paths(root)
            artifacts = RunArtifacts(paths)
            artifacts.ensure_history_log(enabled=True)
            self.assertIn("No persisted history queries", paths.history_queries.read_text(encoding="utf-8"))

            paths.history_queries.write_text("real query\n", encoding="utf-8")
            artifacts.ensure_history_log(enabled=True)
            self.assertEqual(paths.history_queries.read_text(encoding="utf-8"), "real query\n")


if __name__ == "__main__":
    unittest.main()
