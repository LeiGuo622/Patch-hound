from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from history_mcp_probe import run_stdio_probe  # noqa: E402


class HistoryMcpProbeTest(unittest.TestCase):
    def test_stdio_probe_validates_protocol_listing_call_and_log(self) -> None:
        responses = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"serverInfo": {"name": "patchhound_history"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "query_pre_patch_history"}]},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Only target patch ancestors were searched.\nHistory result.",
                        }
                    ],
                    "isError": False,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            query_log = root / "history.md"
            query_log.write_text("recorded\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="\n".join(json.dumps(response) for response in responses),
                stderr="",
            )
            with patch("history_mcp_probe.subprocess.run", return_value=completed) as run:
                result = run_stdio_probe(
                    case_id="case_0001",
                    kind="function_history",
                    file_path="drivers/test.c",
                    symbol="test_function",
                    max_events=1,
                    linux_repo=root,
                    query_log=query_log,
                    timeout_seconds=10,
                )

        self.assertTrue(result["ok"])
        request_lines = run.call_args.kwargs["input"].splitlines()
        call = json.loads(request_lines[2])
        self.assertEqual(call["method"], "tools/call")
        self.assertEqual(call["params"]["name"], "query_pre_patch_history")
        self.assertEqual(call["params"]["arguments"]["analysis_stage"], "patch security review")


if __name__ == "__main__":
    unittest.main()
