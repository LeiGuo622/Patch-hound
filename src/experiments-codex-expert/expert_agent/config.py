"""Configuration shared by the staged PatchHound agent."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values


class ConfigurationError(RuntimeError):
    """Raised when an experiments-codex-expert setting is invalid."""


def find_project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise ConfigurationError("PatchHound project root not found")


PROJECT_ROOT = find_project_root()
EXPERIMENT_ROOT = PROJECT_ROOT / "src" / "experiments-codex-expert"
PROMPTS_ROOT = EXPERIMENT_ROOT / "prompts"
DEFAULT_RUNTIME_CONFIG = EXPERIMENT_ROOT / "codex_runtime.toml"
DEFAULT_CASES_DIR = PROJECT_ROOT / "cases"
DEFAULT_GUIDANCE_ROOT = PROJECT_ROOT / "third_party" / "sashiko" / "third_party" / "prompts" / "kernel"
DEFAULT_HISTORY_SERVER = PROJECT_ROOT / "tools" / "history_mcp_server.py"
DEFAULT_METADATA_DIR = PROJECT_ROOT / "cases" / "case_metadata"
DEFAULT_LINUX_REPO = PROJECT_ROOT / "third_party" / "linux"
DEFAULT_PHOENIX_PROJECT = "patchhound-experiments-codex-expert"


@dataclass(frozen=True)
class PhoenixConfig:
    """Host-side Phoenix configuration; values are not sent to model tools."""

    enabled: bool = True
    collector_endpoint: str = "http://127.0.0.1:6006/v1/traces"
    # Keep all runs for this backend in one dedicated Phoenix project.  The
    # model is recorded as a span attribute, rather than being part of the
    # project name, so changing a model does not split the experiment.
    project_name: str = DEFAULT_PHOENIX_PROJECT
    api_key: str | None = None
    max_tool_output_bytes: int = 128 * 1024

    def effective_project_name(self, model: str) -> str:
        del model  # retained for API compatibility with existing workflow callers
        return self.project_name.strip() or DEFAULT_PHOENIX_PROJECT

    @classmethod
    def load(
        cls,
        *,
        runtime_path: Path = DEFAULT_RUNTIME_CONFIG,
        env_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
        enabled: bool | None = None,
        endpoint: str | None = None,
        project_name: str | None = None,
    ) -> PhoenixConfig:
        runtime = _phoenix_runtime_defaults(runtime_path)
        path = (env_path or PROJECT_ROOT / ".env").expanduser().resolve()
        values = {key: value for key, value in dotenv_values(path).items() if isinstance(value, str)}
        values.update(dict(os.environ if environ is None else environ))
        loaded = cls(
            enabled=_bool_value(values, "PHOENIX_ENABLED", runtime["enabled"]),
            collector_endpoint=(
                values.get("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
                or runtime["collector_endpoint"]
            ),
            project_name=values.get("PHOENIX_PROJECT_NAME", "").strip() or runtime["project_name"],
            api_key=_optional(values.get("PHOENIX_API_KEY")),
            max_tool_output_bytes=_positive_int(
                values.get("PHOENIX_MAX_TOOL_OUTPUT_BYTES"),
                default=runtime["max_tool_output_bytes"],
                name="PHOENIX_MAX_TOOL_OUTPUT_BYTES",
            ),
        )
        return replace(
            loaded,
            enabled=loaded.enabled if enabled is None else enabled,
            collector_endpoint=endpoint or loaded.collector_endpoint,
            project_name=loaded.project_name if project_name is None else project_name,
        )

    def secrets(self, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
        values = dict(os.environ if environ is None else environ)
        candidates = [self.api_key]
        for name, value in values.items():
            upper = name.upper()
            if value and (upper.endswith("_API_KEY") or upper.endswith("_TOKEN") or upper.endswith("_SECRET")):
                candidates.append(value)
        return tuple(dict.fromkeys(value for value in candidates if value))


def _phoenix_runtime_defaults(path: Path) -> dict[str, Any]:
    """Read the local runtime's Phoenix defaults, mirroring omp-simple."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ConfigurationError(f"Phoenix runtime config not found: {resolved}")
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"invalid Phoenix runtime config: {resolved}") from exc
    section = raw.get("phoenix", {})
    if not isinstance(section, dict):
        raise ConfigurationError("[phoenix] must be a TOML table")

    enabled = section.get("enabled", True)
    endpoint = section.get("collector_endpoint", "http://127.0.0.1:6006/v1/traces")
    project = section.get("project_name", DEFAULT_PHOENIX_PROJECT)
    max_output = section.get("max_tool_output_bytes", 128 * 1024)
    if not isinstance(enabled, bool):
        raise ConfigurationError("phoenix.enabled must be a boolean")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ConfigurationError("phoenix.collector_endpoint must be a non-empty string")
    if not isinstance(project, str) or not project.strip():
        raise ConfigurationError("phoenix.project_name must be a non-empty string")
    if not isinstance(max_output, int) or isinstance(max_output, bool) or max_output <= 0:
        raise ConfigurationError("phoenix.max_tool_output_bytes must be a positive integer")
    return {
        "enabled": enabled,
        "collector_endpoint": endpoint.strip(),
        "project_name": project.strip(),
        "max_tool_output_bytes": max_output,
    }


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _bool_value(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _positive_int(value: str | None, *, default: int, name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return parsed
