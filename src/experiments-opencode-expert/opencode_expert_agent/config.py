"""Configuration loading and invariant checks for the OpenCode experiment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


class ConfigurationError(RuntimeError):
    """Raised when a comparable run cannot be configured safely."""


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]


def _project_root() -> Path:
    for parent in EXPERIMENT_ROOT.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "cases").is_dir():
            return parent
    raise ConfigurationError("PatchHound project root could not be located")


PROJECT_ROOT = _project_root()
DEFAULT_CASES_DIR = PROJECT_ROOT / "cases"
PROMPTS_ROOT = EXPERIMENT_ROOT / "prompts"
DEFAULT_PROMPT_TEMPLATE = PROMPTS_ROOT / "task_context.md"
DEFAULT_RUNTIME_CONFIG = EXPERIMENT_ROOT / "opencode_expert_runtime.toml"
DEFAULT_SIDECAR_ENTRY = EXPERIMENT_ROOT / "dist" / "sidecar" / "index.js"
DEFAULT_HISTORY_SERVER = PROJECT_ROOT / "tools" / "history_mcp_server.py"
DEFAULT_METADATA_DIR = PROJECT_ROOT / "cases" / "case_metadata"
DEFAULT_LINUX_REPO = PROJECT_ROOT / "third_party" / "linux"
DEFAULT_GUIDANCE_ROOT = PROJECT_ROOT / "third_party" / "sashiko" / "third_party" / "prompts" / "kernel"
DEEPSEEK_PROVIDER = "newapi"
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash"
DEEPSEEK_MODEL_SELECTOR = f"{DEEPSEEK_PROVIDER}/{DEEPSEEK_MODEL}"
DEEPSEEK_API_ENV = "PATCHHOUND_DEEPSEEK_API_KEY"
GLM_MODEL = "z-ai/glm-5.3"
DEEPSEEK_OFFICIAL_PROVIDER = "deepseek-official"
DEEPSEEK_OFFICIAL_MODEL = "deepseek-flash"
DEEPSEEK_OFFICIAL_API_ENV = "PATCHHOUND_DEEPSEEK_OFFICIAL_API_KEY"
DEEPSEEK_V41_MODEL = "deepseek/deepseek-v4.1-flash"
DEEPSEEK_PINNED_0731_MODEL = "deepseek/deepseek-v4-flash-0731"
SUPPORTED_NEWAPI_MODELS = frozenset(
    {DEEPSEEK_MODEL, DEEPSEEK_V41_MODEL, DEEPSEEK_PINNED_0731_MODEL, GLM_MODEL}
)
OPENAI_ACCOUNT_MODELS = frozenset({"gpt-5.6-sol", "gpt-5.5"})
CODING_PLAN_PROVIDER = "zhipu-coding-plan"
CODING_PLAN_MODEL = "glm-5.3"
CODING_PLAN_MODEL_SELECTOR = f"{CODING_PLAN_PROVIDER}/{CODING_PLAN_MODEL}"
CODING_PLAN_API_ENV = "PATCHHOUND_GLM_CODING_API_KEY"
DGX_PROVIDER = "dgx"
DGX_MODEL = "deepseek-v4-flash-0731"
DGX_MODEL_SELECTOR = f"{DGX_PROVIDER}/{DGX_MODEL}"
DGX_API_ENV = "PATCHHOUND_DGX_API_KEY"


def resolve_linux_repo(explicit: Path | None = None, env_path: Path | None = None) -> Path:
    """Resolve the history repository with experiments-codex-simple precedence."""
    if explicit is not None:
        return explicit.expanduser().resolve()
    for name in ("PATCHHOUND_LINUX_REPO", "LINUX_REPO_PATH"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser().resolve()
    value = dotenv_values((env_path or PROJECT_ROOT / ".env").expanduser().resolve()).get("LINUX_REPO_PATH")
    if isinstance(value, str) and value.strip():
        return Path(value.strip()).expanduser().resolve()
    return DEFAULT_LINUX_REPO.resolve()


@dataclass(frozen=True)
class LimitsConfig:
    message_bytes: int
    stderr_bytes: int
    tool_output_bytes: int
    event_output_bytes: int
    total_event_bytes: int


@dataclass(frozen=True)
class RuntimeConfig:
    path: Path
    logical_model: str
    model_selector: str
    provider: str
    reasoning_effort: str
    timeout_seconds: int
    max_contract_repairs: int
    history_mcp_required: bool
    network_enabled: bool
    output_dir_name: str
    cli_version: str
    sdk_version: str
    sidecar_protocol_version: int
    server_ready_timeout_seconds: int
    mcp_timeout_ms: int
    dispose_grace_seconds: int
    auth_required: bool
    enabled_tools: tuple[str, ...]
    disabled_tools: tuple[str, ...]
    limits: LimitsConfig

    @classmethod
    def load(cls, path: Path = DEFAULT_RUNTIME_CONFIG) -> RuntimeConfig:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
        experiment = _table(raw, "experiment")
        runtime = _table(raw, "runtime")
        tools = _table(raw, "tools")
        limits = _table(raw, "limits")
        result = cls(
            path=resolved,
            logical_model=_string(experiment, "logical_model"),
            model_selector=_string(experiment, "model_selector"),
            provider=_string(experiment, "provider"),
            reasoning_effort=_string(experiment, "reasoning_effort"),
            timeout_seconds=_positive_int(experiment, "timeout_seconds"),
            max_contract_repairs=_nonnegative_int(experiment, "max_contract_repairs"),
            history_mcp_required=_boolean(experiment, "history_mcp_required"),
            network_enabled=_boolean(experiment, "network_enabled"),
            output_dir_name=_string(experiment, "output_dir_name"),
            cli_version=_string(runtime, "cli_version"),
            sdk_version=_string(runtime, "sdk_version"),
            sidecar_protocol_version=_positive_int(runtime, "sidecar_protocol_version"),
            server_ready_timeout_seconds=_positive_int(runtime, "server_ready_timeout_seconds"),
            mcp_timeout_ms=_positive_int(runtime, "mcp_timeout_ms"),
            dispose_grace_seconds=_positive_int(runtime, "dispose_grace_seconds"),
            auth_required=_boolean(runtime, "auth_required"),
            enabled_tools=_string_tuple(tools, "enabled"),
            disabled_tools=_string_tuple(tools, "disabled"),
            limits=LimitsConfig(
                message_bytes=_positive_int(limits, "message_bytes"),
                stderr_bytes=_positive_int(limits, "stderr_bytes"),
                tool_output_bytes=_positive_int(limits, "tool_output_bytes"),
                event_output_bytes=_positive_int(limits, "event_output_bytes"),
                total_event_bytes=_positive_int(limits, "total_event_bytes"),
            ),
        )
        result.validate_invariants()
        return result

    def validate_invariants(self) -> None:
        is_newapi_model = (
            self.logical_model in SUPPORTED_NEWAPI_MODELS
            and self.model_selector == f"{DEEPSEEK_PROVIDER}/{self.logical_model}"
            and self.provider == DEEPSEEK_PROVIDER
        )
        is_coding_plan_model = (
            self.logical_model == CODING_PLAN_MODEL
            and self.model_selector == CODING_PLAN_MODEL_SELECTOR
            and self.provider == CODING_PLAN_PROVIDER
        )
        is_dgx_model = (
            self.logical_model == DGX_MODEL
            and self.model_selector == DGX_MODEL_SELECTOR
            and self.provider == DGX_PROVIDER
        )
        is_api_key_model = is_newapi_model or is_coding_plan_model or is_dgx_model
        is_official_deepseek_model = (
            self.logical_model == DEEPSEEK_OFFICIAL_MODEL
            and self.model_selector == f"{DEEPSEEK_OFFICIAL_PROVIDER}/{DEEPSEEK_OFFICIAL_MODEL}"
            and self.provider == DEEPSEEK_OFFICIAL_PROVIDER
        )
        is_openai_account_model = (
            self.logical_model in OPENAI_ACCOUNT_MODELS
            and self.model_selector == f"openai/{self.logical_model}"
            and self.provider == "openai"
        )
        if not is_api_key_model and not is_openai_account_model and not is_official_deepseek_model:
            raise ConfigurationError(
                "logical_model/provider/model_selector must form a supported "
                "account-authenticated or API-key model tuple"
            )
        if self.reasoning_effort != "high":
            raise ConfigurationError("reasoning_effort must remain high")
        if self.timeout_seconds not in {7200, 10_800}:
            raise ConfigurationError("formal timeout must remain 7200 or 10800 seconds")
        if self.max_contract_repairs != 0:
            raise ConfigurationError("expert workflow must not use contract repair turns")
        if not self.history_mcp_required:
            raise ConfigurationError("history MCP must be required")
        if self.network_enabled:
            raise ConfigurationError("Agent tool network must remain disabled")
        if self.output_dir_name != "opencode-expert-runs":
            raise ConfigurationError("output_dir_name must remain opencode-expert-runs")
        if self.cli_version != self.sdk_version:
            raise ConfigurationError("OpenCode CLI and SDK versions must match")
        if self.server_ready_timeout_seconds != 15:
            raise ConfigurationError("formal server_ready_timeout_seconds must remain 15")
        if self.mcp_timeout_ms != 30_000:
            raise ConfigurationError("formal mcp_timeout_ms must remain 30000")
        overlap = set(self.enabled_tools) & set(self.disabled_tools)
        if overlap:
            raise ConfigurationError(f"tools cannot be both enabled and disabled: {sorted(overlap)}")
        forbidden = {"apply_patch", "edit", "write", "task", "skill", "lsp"}
        if forbidden & set(self.enabled_tools):
            raise ConfigurationError("write, delegation, skill, and LSP tools must not be enabled")


def provider_base_url(provider: str) -> str:
    """Read a self-hosted/provider endpoint from the host environment or .env."""
    settings = {
        DEEPSEEK_PROVIDER: ("PATCHHOUND_NEWAPI_BASE_URL", "https://api.example.invalid/v1"),
        DGX_PROVIDER: ("PATCHHOUND_DGX_BASE_URL", "http://127.0.0.1:8000/v1"),
        CODING_PLAN_PROVIDER: ("PATCHHOUND_GLM_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4"),
    }.get(provider)
    if settings is None:
        return ""
    key, default = settings
    value = os.environ.get(key, "").strip() or str(dotenv_values(PROJECT_ROOT / ".env").get(key) or "").strip() or default
    if not value.startswith(("http://", "https://")) or any(char.isspace() for char in value):
        raise ConfigurationError(f"{key} must be an HTTP(S) URL without whitespace")
    return value


def resolve_provider_environment(config: RuntimeConfig) -> dict[str, str]:
    """Resolve NewAPI credentials for the isolated outer sidecar only."""

    api_env = {
        DEEPSEEK_PROVIDER: DEEPSEEK_API_ENV,
        CODING_PLAN_PROVIDER: CODING_PLAN_API_ENV,
        DEEPSEEK_OFFICIAL_PROVIDER: DEEPSEEK_OFFICIAL_API_ENV,
        DGX_PROVIDER: DGX_API_ENV,
    }.get(config.provider)
    if api_env is None:
        return {}
    direct = os.environ.get(api_env, "").strip()
    dotenv = dotenv_values(PROJECT_ROOT / ".env").get(api_env)
    value = direct or (dotenv.strip() if isinstance(dotenv, str) else "")
    if not value:
        raise ConfigurationError(f"{api_env} is required for provider {config.provider}")
    if any(char in value for char in "\r\n\x00"):
        raise ConfigurationError(f"{api_env} contains invalid characters")
    return {api_env: value}


@dataclass(frozen=True)
class PhoenixConfig:
    enabled: bool = True
    collector_endpoint: str = "http://127.0.0.1:6006/v1/traces"
    project_name: str = "patchhound-experiments-opencode-expert"
    api_key: str | None = None
    max_tool_output_bytes: int = 128 * 1024

    @classmethod
    def load(
        cls,
        *,
        runtime_path: Path = DEFAULT_RUNTIME_CONFIG,
        env_path: Path | None = None,
        enabled: bool | None = None,
        endpoint: str | None = None,
        project_name: str | None = None,
    ) -> PhoenixConfig:
        raw = tomllib.loads(runtime_path.expanduser().resolve().read_text(encoding="utf-8"))
        phoenix = _table(raw, "phoenix")
        env_file = dotenv_values((env_path or PROJECT_ROOT / ".env").expanduser().resolve())

        def value(name: str) -> str | None:
            direct = os.environ.get(name)
            if direct is not None:
                return direct
            candidate = env_file.get(name)
            return candidate if isinstance(candidate, str) else None

        configured = _parse_bool(value("PHOENIX_ENABLED"), default=_boolean(phoenix, "enabled"))
        return cls(
            enabled=configured if enabled is None else enabled,
            collector_endpoint=endpoint or value("PHOENIX_COLLECTOR_ENDPOINT") or _string(phoenix, "collector_endpoint"),
            project_name=project_name or value("PHOENIX_PROJECT_NAME") or _string(phoenix, "project_name"),
            api_key=value("PHOENIX_API_KEY"),
            max_tool_output_bytes=_positive_int(phoenix, "max_tool_output_bytes"),
        )

    def secrets(self) -> tuple[str, ...]:
        candidates = [self.api_key]
        for name, value in os.environ.items():
            if value and name.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET")):
                candidates.append(value)
        return tuple(dict.fromkeys(value for value in candidates if value))


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"missing TOML table: {key}")
    return value


def _string(table: dict[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def _boolean(table: dict[str, Any], key: str) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def _positive_int(table: dict[str, Any], key: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"{key} must be a positive integer")
    return value


def _nonnegative_int(table: dict[str, Any], key: str) -> int:
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{key} must be a non-negative integer")
    return value


def _string_tuple(table: dict[str, Any], key: str) -> tuple[str, ...]:
    value = table.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigurationError(f"{key} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ConfigurationError(f"{key} contains duplicate values")
    return tuple(value)


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"invalid boolean value: {value}")
