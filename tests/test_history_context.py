from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tools.history_context import (  # noqa: E402
    DEFAULT_MAX_EVENTS,
    query_pre_patch_history,
    sanitize_history_text,
    validate_query,
)
from tools.history_context import (  # noqa: E402
    main as history_context_main,
)


class HistoryContextTest(unittest.TestCase):
    def test_validate_query_rejects_unsafe_agent_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            validate_query(kind="function_history", file_path="/drivers/foo.c", symbol="foo")
        with self.assertRaisesRegex(ValueError, "must not contain"):
            validate_query(kind="function_history", file_path="../drivers/foo.c", symbol="foo")
        with self.assertRaisesRegex(ValueError, "already be normalized"):
            validate_query(kind="function_history", file_path="drivers//foo.c", symbol="foo")
        with self.assertRaisesRegex(ValueError, "simple C identifier"):
            validate_query(kind="function_history", file_path="drivers/foo.c", symbol="foo;git")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate_query(kind="regex_history", file_path="drivers/foo.c", symbol="foo")
        with self.assertRaisesRegex(ValueError, "requires symbol"):
            validate_query(kind="symbol_history", file_path="drivers/foo.c")
        with self.assertRaisesRegex(ValueError, "does not accept symbol"):
            validate_query(kind="file_history", file_path="drivers/foo.c", symbol="foo")

        query = validate_query(
            kind="function_history",
            file_path="drivers/foo.c",
            symbol="foo",
            max_events=999,
        )
        self.assertEqual(query.max_events, DEFAULT_MAX_EVENTS)

    def test_sanitize_history_text_removes_identity_revision_and_link_metadata(self) -> None:
        raw = "\n".join(
            [
                "commit 0123456789abcdef0123456789abcdef01234567",
                "Author: Dev <dev@example.com>",
                "Date: Thu Jan 1 00:00:00 1970 +0000",
                "Fixes: 1111111111111111111111111111111111111111",
                "Link: https://example.invalid/bug",
                "index 123456789abcd..abcdef0123456 100644",
                "+ contact security@example.com about 2222222222222222222222222222222222222222",
            ]
        )

        sanitized = sanitize_history_text(raw)

        self.assertNotIn("Author:", sanitized)
        self.assertNotIn("Date:", sanitized)
        self.assertNotIn("Fixes:", sanitized)
        self.assertNotIn("Link:", sanitized)
        self.assertNotIn("dev@example.com", sanitized)
        self.assertNotIn("security@example.com", sanitized)
        self.assertNotIn("0123456789abcdef0123456789abcdef01234567", sanitized)
        self.assertIn("index <blob>..<blob>", sanitized)
        self.assertIn("<email>", sanitized)
        self.assertIn("<commit>", sanitized)

    def test_function_history_reads_only_target_parent_and_older_history(self) -> None:
        with self._sample_case() as sample:
            result = query_pre_patch_history(
                "case_0001",
                kind="function_history",
                file_path="drivers/foo.c",
                symbol="foo_submit",
                linux_repo=sample.linux_repo,
                metadata_dir=sample.metadata_dir,
                max_events=5,
            )

        self.assertGreaterEqual(result.event_count, 1)
        self.assertIn("drivers: bound foo submit length", result.text)
        self.assertIn("if (len > FOO_LIMIT)", result.text)
        self.assertIn("bounds-check", result.text)
        self.assertIn("Invariant candidates:", result.text)
        self.assertNotIn("target patch should not appear", result.text)
        self.assertNotIn(sample.target_commit, result.text)
        self.assertNotIn("reviewer@example.com", result.text)
        self.assertNotIn("Signed-off-by", result.text)
        self.assertNotIn("Fixes:", result.text)
        self.assertNotIn("Link:", result.text)

    def test_symbol_and_file_history_return_sanitized_diff_context(self) -> None:
        with self._sample_case() as sample:
            symbol_result = query_pre_patch_history(
                "case_0001",
                kind="symbol_history",
                file_path="drivers/foo.c",
                symbol="FOO_LIMIT",
                linux_repo=sample.linux_repo,
                metadata_dir=sample.metadata_dir,
                max_events=5,
            )
            file_result = query_pre_patch_history(
                "case_0001",
                kind="file_history",
                file_path="drivers/foo.c",
                linux_repo=sample.linux_repo,
                metadata_dir=sample.metadata_dir,
                max_events=1,
            )

        self.assertIn("drivers: bound foo submit length", symbol_result.text)
        self.assertIn("FOO_LIMIT", symbol_result.text)
        self.assertIn("index <blob>..<blob>", file_result.text)
        self.assertNotIn("target patch should not appear", symbol_result.text)
        self.assertNotIn("target patch should not appear", file_result.text)

    def test_cli_prints_query_result(self) -> None:
        with self._sample_case() as sample:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = history_context_main(
                    [
                        "case_0001",
                        "--kind",
                        "function_history",
                        "--file",
                        "drivers/foo.c",
                        "--symbol",
                        "foo_submit",
                        "--linux-repo",
                        str(sample.linux_repo),
                        "--metadata-dir",
                        str(sample.metadata_dir),
                    ]
                )
            query_log = sample.case_output_dir / "history_queries.md"
            self.assertTrue(query_log.is_file())
            query_log_text = query_log.read_text(encoding="utf-8")

        self.assertEqual(status, 0, stderr.getvalue())
        self.assertIn("# Pre-Patch History Query", stdout.getvalue())
        self.assertIn("foo_submit", stdout.getvalue())
        self.assertIn("| 1 | case_0001 | function_history | drivers/foo.c | foo_submit | 5 | yes |", query_log_text)
        self.assertIn("## Query 1 Result", query_log_text)
        self.assertIn("Returned context:", query_log_text)
        self.assertIn("drivers: bound foo submit length", query_log_text)
        self.assertNotIn(sample.target_commit, query_log_text)
        self.assertNotIn(str(sample.linux_repo), query_log_text)

    def test_mcp_server_returns_history_and_writes_sanitized_log(self) -> None:
        with self._sample_case() as sample:
            log_path = sample.case_output_dir / "mcp_history_queries.md"
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "query_pre_patch_history",
                        "arguments": {
                            "kind": "function_history",
                            "file_path": "drivers/foo.c",
                            "symbol": "foo_submit",
                            "max_events": 2,
                            "analysis_stage": "Stage 2 execution flow",
                            "reason": "Check whether older execution-flow bounds were intentional.",
                        },
                    },
                },
            ]
            env = os.environ.copy()
            env.update(
                {
                    "PATCHHOUND_CASE_ID": "case_0001",
                    "PATCHHOUND_PROJECT_ROOT": str(PROJECT_ROOT),
                    "PATCHHOUND_METADATA_DIR": str(sample.metadata_dir),
                    "PATCHHOUND_LINUX_REPO": str(sample.linux_repo),
                    "PATCHHOUND_HISTORY_QUERY_LOG": str(log_path),
                }
            )
            completed = subprocess.run(
                [sys.executable, "-B", str(PROJECT_ROOT / "tools" / "history_mcp_server.py")],
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            initialize_result = responses[0]["result"]
            list_result = responses[1]["result"]
            listed_tools = [
                tool["name"]
                for response in responses
                for tool in response.get("result", {}).get("tools", [])
            ]
            returned_text = "\n".join(
                item["text"]
                for response in responses
                for item in response.get("result", {}).get("content", [])
            )
            query_log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(initialize_result["protocolVersion"], "2025-06-18")
        self.assertEqual(initialize_result["capabilities"]["tools"], {"listChanged": False})
        # MCP list cursors are optional, not nullable.  OpenCode's strict MCP
        # schema rejects ``nextCursor: null``, so an exhausted result omits it.
        self.assertNotIn("nextCursor", list_result)
        self.assertIn("query_pre_patch_history", listed_tools)
        self.assertIn("# Pre-Patch History Query", returned_text)
        self.assertIn("drivers: bound foo submit length", returned_text)
        self.assertIn("stage=Stage 2 execution flow", query_log_text)
        self.assertIn("Check whether older execution-flow bounds were intentional.", query_log_text)
        self.assertIn("Returned context:", query_log_text)
        self.assertNotIn(sample.target_commit, returned_text)
        self.assertNotIn(sample.target_commit, query_log_text)
        self.assertNotIn(str(sample.linux_repo), query_log_text)

    @contextlib.contextmanager
    def _sample_case(self):
        with tempfile.TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            linux_repo = temp_dir / "linux"
            linux_repo.mkdir()
            self._git(linux_repo, "init")
            self._git(linux_repo, "config", "user.email", "dev@example.com")
            self._git(linux_repo, "config", "user.name", "Dev")

            source_file = linux_repo / "drivers" / "foo.c"
            source_file.parent.mkdir()
            source_file.write_text(
                "#define FOO_LIMIT 4\n\n"
                "int foo_submit(int len)\n"
                "{\n"
                "\treturn len;\n"
                "}\n",
                encoding="utf-8",
            )
            self._git(linux_repo, "add", "drivers/foo.c")
            self._git(
                linux_repo,
                "commit",
                "-m",
                "drivers: add foo submit\n\n"
                "Link: https://example.invalid/initial\n"
                "Signed-off-by: Dev <dev@example.com>",
            )

            source_file.write_text(
                "#define FOO_LIMIT 8\n\n"
                "int foo_submit(int len)\n"
                "{\n"
                "\tif (len > FOO_LIMIT)\n"
                "\t\treturn -1;\n"
                "\treturn len;\n"
                "}\n",
                encoding="utf-8",
            )
            self._git(linux_repo, "add", "drivers/foo.c")
            self._git(
                linux_repo,
                "commit",
                "-m",
                "drivers: bound foo submit length\n\n"
                "Fixes: 0123456789abcdef0123456789abcdef01234567\n"
                "Reviewed-by: Reviewer <reviewer@example.com>",
            )

            source_file.write_text(
                "#define FOO_LIMIT 8\n\n"
                "int foo_submit(int len)\n"
                "{\n"
                "\tif (len > FOO_LIMIT)\n"
                "\t\treturn -1;\n"
                "\treturn len + 1;\n"
                "}\n",
                encoding="utf-8",
            )
            self._git(linux_repo, "add", "drivers/foo.c")
            self._git(linux_repo, "commit", "-m", "target patch should not appear")
            target_commit = self._git(linux_repo, "rev-parse", "HEAD").strip()

            case_source_dir = temp_dir / "cases" / "case_0001" / "source"
            case_output_dir = temp_dir / "cases" / "case_0001" / "output"
            (case_source_dir / "drivers").mkdir(parents=True)
            case_output_dir.mkdir()
            (case_source_dir / "drivers" / "foo.c").write_text(
                source_file.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            metadata_dir = temp_dir / "cases" / "case_metadata"
            metadata_dir.mkdir()
            (metadata_dir / "case_0001.json").write_text(
                json.dumps(
                    {
                        "case_id": "case_0001",
                        "commit_id": target_commit,
                        "source_dir": str(case_source_dir),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            yield _SampleCase(
                linux_repo=linux_repo,
                metadata_dir=metadata_dir,
                target_commit=target_commit,
                case_output_dir=case_output_dir,
            )

    def _git(self, repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout


class _SampleCase:
    def __init__(
        self,
        *,
        linux_repo: Path,
        metadata_dir: Path,
        target_commit: str,
        case_output_dir: Path,
    ) -> None:
        self.linux_repo = linux_repo
        self.metadata_dir = metadata_dir
        self.target_commit = target_commit
        self.case_output_dir = case_output_dir


if __name__ == "__main__":
    unittest.main()
