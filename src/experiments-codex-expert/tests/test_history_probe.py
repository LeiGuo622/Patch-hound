from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

import history_mcp_probe  # noqa: E402


class HistoryProbeTest(unittest.TestCase):
    def test_query_arguments_include_stage_and_reason(self) -> None:
        value = history_mcp_probe._query_arguments("function_history", "a.c", "work", 2)
        self.assertEqual(value["kind"], "function_history")
        self.assertEqual(value["symbol"], "work")
        self.assertEqual(value["analysis_stage"], "patch security review")
        self.assertTrue(value["reason"])

    def test_temporary_paths_match_expert_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = history_mcp_probe._temporary_run_paths(Path(temporary))
            self.assertEqual(paths.history_queries.name, "history_queries.md")
            self.assertFalse(hasattr(paths, "invalid_response"))


if __name__ == "__main__":
    unittest.main()
