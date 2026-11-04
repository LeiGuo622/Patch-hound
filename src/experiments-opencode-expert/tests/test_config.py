from __future__ import annotations

from pathlib import Path

import pytest

from opencode_expert_agent.config import (
    DEFAULT_RUNTIME_CONFIG,
    ConfigurationError,
    RuntimeConfig,
    resolve_linux_repo,
    resolve_provider_environment,
)

DEEPSEEK_RUNTIME_CONFIG = DEFAULT_RUNTIME_CONFIG.with_name(
    "opencode_expert_runtime.newapi-deepseek-v4-flash.toml"
)
GLM_RUNTIME_CONFIG = DEFAULT_RUNTIME_CONFIG.with_name(
    "opencode_expert_runtime.newapi-glm-5.3.toml"
)


def test_formal_runtime_invariants_are_locked() -> None:
    config = RuntimeConfig.load()
    assert config.logical_model == "gpt-5.6-sol"
    assert config.model_selector == "openai/gpt-5.6-sol"
    assert config.reasoning_effort == "high"
    assert config.timeout_seconds == 7200
    assert config.max_contract_repairs == 0
    assert config.cli_version == config.sdk_version == "1.18.25"
    assert config.mcp_timeout_ms == 30_000
    assert config.output_dir_name == "opencode-expert-runs"


def test_changed_formal_invariant_is_rejected(tmp_path: Path) -> None:
    poisoned = tmp_path / "runtime.toml"
    poisoned.write_text(
        DEFAULT_RUNTIME_CONFIG.read_text(encoding="utf-8").replace('reasoning_effort = "high"', 'reasoning_effort = "low"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="reasoning_effort"):
        RuntimeConfig.load(poisoned)


def test_deepseek_runtime_preserves_expert_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig.load(DEEPSEEK_RUNTIME_CONFIG)
    assert config.logical_model == "deepseek/deepseek-v4-flash"
    assert config.model_selector == "newapi/deepseek/deepseek-v4-flash"
    assert config.provider == "newapi"
    assert config.reasoning_effort == "high"
    assert config.max_contract_repairs == 0
    assert config.output_dir_name == "opencode-expert-runs"
    assert config.auth_required is False
    assert config.timeout_seconds == 7200
    monkeypatch.setenv("PATCHHOUND_DEEPSEEK_API_KEY", "provider-secret")
    assert resolve_provider_environment(config) == {
        "PATCHHOUND_DEEPSEEK_API_KEY": "provider-secret"
    }


def test_glm_runtime_only_changes_model_identity_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig.load(GLM_RUNTIME_CONFIG)
    assert config.logical_model == "z-ai/glm-5.3"
    assert config.model_selector == "newapi/z-ai/glm-5.3"
    assert config.provider == "newapi"
    assert config.reasoning_effort == "high"
    assert config.timeout_seconds == 7200
    assert config.max_contract_repairs == 0
    assert config.output_dir_name == "opencode-expert-runs"
    assert config.auth_required is False
    monkeypatch.setenv("PATCHHOUND_DEEPSEEK_API_KEY", "provider-secret")
    assert resolve_provider_environment(config) == {
        "PATCHHOUND_DEEPSEEK_API_KEY": "provider-secret"
    }


def test_deepseek_model_tuple_cannot_be_mixed(tmp_path: Path) -> None:
    poisoned = tmp_path / "runtime.toml"
    poisoned.write_text(
        DEEPSEEK_RUNTIME_CONFIG.read_text(encoding="utf-8").replace(
            'model_selector = "newapi/deepseek/deepseek-v4-flash"',
            'model_selector = "openai/gpt-5.6-sol"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="logical_model"):
        RuntimeConfig.load(poisoned)


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
