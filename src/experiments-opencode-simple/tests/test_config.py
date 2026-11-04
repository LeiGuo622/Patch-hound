from __future__ import annotations

from pathlib import Path

import pytest

from opencode_simple_agent.config import (
    DEFAULT_RUNTIME_CONFIG,
    EXPERIMENT_ROOT,
    ConfigurationError,
    RuntimeConfig,
    resolve_linux_repo,
)


def test_formal_runtime_invariants_are_locked() -> None:
    config = RuntimeConfig.load()
    assert config.logical_model == "gpt-5.6-sol"
    assert config.model_selector == "openai/gpt-5.6-sol"
    assert config.reasoning_effort == "high"
    assert config.timeout_seconds == 7200
    assert config.max_contract_repairs == 2
    assert config.cli_version == config.sdk_version == "1.18.25"
    assert config.mcp_timeout_ms == 30_000
    assert config.output_dir_name == "opencode-runs"


def test_changed_formal_invariant_is_rejected(tmp_path: Path) -> None:
    poisoned = tmp_path / "runtime.toml"
    poisoned.write_text(
        DEFAULT_RUNTIME_CONFIG.read_text(encoding="utf-8").replace('reasoning_effort = "high"', 'reasoning_effort = "low"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="reasoning_effort"):
        RuntimeConfig.load(poisoned)


def test_glm53_runtime_preserves_formal_invariants() -> None:
    config = RuntimeConfig.load(EXPERIMENT_ROOT / "opencode_runtime.newapi-glm-5.3.toml")
    assert config.logical_model == "z-ai/glm-5.3"
    assert config.model_selector == "newapi/z-ai/glm-5.3"
    assert config.provider == "newapi"
    assert config.reasoning_effort == "high"
    assert config.timeout_seconds == 7200
    assert config.max_contract_repairs == 2
    assert config.output_dir_name == "opencode-runs"


def test_linux_repo_resolution_matches_baseline_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit"
    environment = tmp_path / "environment"
    dotenv = tmp_path / "dotenv"
    for path in (explicit, environment, dotenv):
        path.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(f"LINUX_REPO_PATH={dotenv}\n", encoding="utf-8")
    monkeypatch.setenv("LINUX_REPO_PATH", str(environment))
    assert resolve_linux_repo(explicit, env_file) == explicit.resolve()
    assert resolve_linux_repo(None, env_file) == environment.resolve()
    monkeypatch.delenv("LINUX_REPO_PATH")
    assert resolve_linux_repo(None, env_file) == dotenv.resolve()
