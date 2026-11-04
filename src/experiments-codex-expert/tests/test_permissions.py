from __future__ import annotations

import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))

from expert_agent.codex_runtime import (  # noqa: E402
    CODEX_CONFIG_OVERRIDES,
    HISTORY_MCP_SERVER_NAME,
    _prepare_external_runtime_config,
    build_session_config,
    prepare_isolated_environment,
    runtime_provider_env_key,
)
from expert_agent.config import DEFAULT_RUNTIME_CONFIG  # noqa: E402

DEEPSEEK_RUNTIME_CONFIG = EXPERIMENT_ROOT / "codex_runtime.deepseek.toml"


class PermissionsTest(unittest.TestCase):
    def test_default_runtime_matches_read_only_network_disabled_contract(self) -> None:
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
        self.assertEqual(config["approval_policy"], "never")

    def test_history_mcp_is_required_when_enabled(self) -> None:
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

    def test_external_landlock_session_uses_wrapper_boundary_and_hides_auth_home(self) -> None:
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
            parsed = tomllib.loads(target.read_text(encoding="utf-8"))

        self.assertNotIn("default_permissions", parsed)
        self.assertNotIn("permissions", parsed)
        self.assertEqual(parsed["sandbox_mode"], "workspace-write")
        self.assertFalse(parsed["sandbox_workspace_write"]["network_access"])
        self.assertEqual(parsed["web_search"], "disabled")
        self.assertEqual(parsed["model_provider"], "openai")

    def test_app_server_override_disables_native_web_search(self) -> None:
        self.assertEqual(CODEX_CONFIG_OVERRIDES, ('web_search="disabled"',))

    def test_deepseek_external_landlock_hides_provider_key_from_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            scratch.mkdir()
            config = build_session_config(
                DEEPSEEK_RUNTIME_CONFIG,
                scratch_dir=scratch,
                codex_bin=None,
                use_history_tool=False,
                case_id="case_0001",
                history_linux_repo=root,
                history_query_log=root / "history.md",
                sandbox_backend="external-landlock",
            )
        self.assertIn("PATCHHOUND_DEEPSEEK_API_KEY", config["shell_environment_policy"]["exclude"])

    def test_each_isolated_environment_has_only_auth_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "auth.json").write_text("{}\n", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(source), "OPENAI_API_KEY": "secret"}, clear=False):
                first = prepare_isolated_environment(root / "first", DEFAULT_RUNTIME_CONFIG)
                second = prepare_isolated_environment(root / "second", DEFAULT_RUNTIME_CONFIG)
            self.assertNotEqual(first.codex_home, second.codex_home)
            self.assertNotEqual(first.scratch_dir, second.scratch_dir)
            self.assertEqual({path.name for path in first.codex_home.iterdir()}, {"auth.json", "config.toml"})
            self.assertNotIn("OPENAI_API_KEY", first.env)

    def test_deepseek_environment_uses_provider_key_without_codex_login(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_home = root / "missing-codex-home"
            with patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(missing_home),
                    "PATCHHOUND_DEEPSEEK_API_KEY": "provider-secret",
                },
                clear=False,
            ):
                isolated = prepare_isolated_environment(root / "run", DEEPSEEK_RUNTIME_CONFIG)
            self.assertEqual(isolated.env["PATCHHOUND_DEEPSEEK_API_KEY"], "provider-secret")
            self.assertEqual({path.name for path in isolated.codex_home.iterdir()}, {"config.toml"})
            runtime = tomllib.loads((isolated.codex_home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(runtime_provider_env_key(runtime), "PATCHHOUND_DEEPSEEK_API_KEY")


if __name__ == "__main__":
    unittest.main()
