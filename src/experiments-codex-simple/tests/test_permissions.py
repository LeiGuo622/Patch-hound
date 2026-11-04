from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from simple_agent.codex_runtime import (  # noqa: E402
    CODEX_CONFIG_OVERRIDES,
    HISTORY_MCP_SERVER_NAME,
    _prepare_external_runtime_config,
    build_session_config,
    prepare_isolated_environment,
)
from simple_agent.config import DEFAULT_RUNTIME_CONFIG  # noqa: E402


class PermissionsTest(unittest.TestCase):
    def test_default_runtime_is_read_only_with_network_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            scratch.mkdir()
            config = build_session_config(
                DEFAULT_RUNTIME_CONFIG,
                scratch_dir=scratch,
                codex_bin=None,
                use_history_tool=False,
                case_id="case_0001",
                history_linux_repo=root,
                history_query_log=root / "history.md",
            )

        profile = config["permissions"]["patch_review"]
        self.assertEqual(profile["filesystem"][":workspace_roots"]["."], "read")
        self.assertEqual(profile["filesystem"][str(scratch.resolve())], "write")
        self.assertFalse(profile["network"]["enabled"])
        self.assertEqual(config["web_search"], "disabled")
        self.assertNotIn("use_legacy_landlock", config["features"])
        self.assertNotIn("sandbox_mode", config)
        self.assertEqual(config["approval_policy"], "never")
        self.assertNotIn("mcp_servers", config)

    def test_history_mcp_is_injected_as_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            scratch.mkdir()
            config = build_session_config(
                DEFAULT_RUNTIME_CONFIG,
                scratch_dir=scratch,
                codex_bin=None,
                use_history_tool=True,
                case_id="case_0001",
                history_linux_repo=root,
                history_query_log=root / "history.md",
            )

        server = config["mcp_servers"][HISTORY_MCP_SERVER_NAME]
        self.assertTrue(server["required"])
        self.assertEqual(server["default_tools_approval_mode"], "approve")
        self.assertEqual(server["env"]["PATCHHOUND_CASE_ID"], "case_0001")
        self.assertEqual(server["env"]["PATCHHOUND_HISTORY_QUERY_LOG"], str((root / "history.md").resolve()))

    def test_external_landlock_uses_legacy_workspace_policy_for_inner_seccomp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            scratch.mkdir()
            config = build_session_config(
                DEFAULT_RUNTIME_CONFIG,
                scratch_dir=scratch,
                codex_bin=None,
                use_history_tool=False,
                case_id="case_0001",
                history_linux_repo=root,
                history_query_log=root / "history.md",
                sandbox_backend="external-landlock",
            )

        self.assertEqual(config["sandbox_mode"], "workspace-write")
        self.assertNotIn("default_permissions", config)
        self.assertNotIn("permissions", config)
        workspace = config["sandbox_workspace_write"]
        self.assertEqual(workspace["writable_roots"], [str(scratch.resolve())])
        self.assertFalse(workspace["network_access"])
        self.assertEqual(config["web_search"], "disabled")
        self.assertTrue(config["features"]["use_legacy_landlock"])
        policy = config["shell_environment_policy"]
        self.assertEqual(policy["set"]["CODEX_HOME"], "/nonexistent")
        self.assertIn("CODEX_HOME", policy["exclude"])

    def test_external_runtime_copy_drops_profiles_but_keeps_provider_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            target.write_text(DEFAULT_RUNTIME_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            _prepare_external_runtime_config(target)
            parsed = __import__("tomllib").loads(target.read_text(encoding="utf-8"))

        self.assertNotIn("default_permissions", parsed)
        self.assertNotIn("permissions", parsed)
        self.assertEqual(parsed["sandbox_mode"], "workspace-write")
        self.assertFalse(parsed["sandbox_workspace_write"]["network_access"])
        self.assertEqual(parsed["web_search"], "disabled")
        self.assertEqual(parsed["model_provider"], "openai")

    def test_app_server_override_disables_native_web_search(self) -> None:
        self.assertEqual(CODEX_CONFIG_OVERRIDES, ('web_search="disabled"',))

    def test_isolated_environment_copies_only_auth_and_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_home = root / "source-home"
            source_home.mkdir()
            (source_home / "auth.json").write_text('{"token":"local"}\n', encoding="utf-8")
            (source_home / "skills").mkdir()
            with patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(source_home),
                    "PHOENIX_API_KEY": "do-not-forward",
                    "OPENAI_API_KEY": "do-not-forward-either",
                },
                clear=False,
            ):
                isolated = prepare_isolated_environment(root / "runtime", DEFAULT_RUNTIME_CONFIG)

            names = {path.name for path in isolated.codex_home.iterdir()}
            self.assertEqual(names, {"auth.json", "config.toml"})
            self.assertNotIn("PHOENIX_API_KEY", isolated.env)
            self.assertNotIn("OPENAI_API_KEY", isolated.env)
            self.assertEqual(isolated.env["TMPDIR"], str(isolated.scratch_dir))


if __name__ == "__main__":
    unittest.main()
