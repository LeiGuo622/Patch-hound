"""Configuration shared by the simplified PatchHound agent."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


class ConfigurationError(RuntimeError):
    """Raised when a local experiments-codex-simple setting is invalid."""


def find_project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise ConfigurationError("PatchHound project root not found")


PROJECT_ROOT = find_project_root()
EXPERIMENT_ROOT = PROJECT_ROOT / "src" / "experiments-codex-simple"
DEFAULT_CASES_DIR = PROJECT_ROOT / "cases"
DEFAULT_PROMPT_TEMPLATE = EXPERIMENT_ROOT / "prompts" / "patch_verification.md"
DEFAULT_RUNTIME_CONFIG = EXPERIMENT_ROOT / "codex_runtime.toml"
DEFAULT_HISTORY_SERVER = PROJECT_ROOT / "tools" / "history_mcp_server.py"
DEFAULT_METADATA_DIR = PROJECT_ROOT / "cases" / "case_metadata"
DEFAULT_LINUX_REPO = PROJECT_ROOT / "third_party" / "linux"


@dataclass(frozen=True)
class PhoenixConfig:
    """Host-side Phoenix configuration. These values are not sent to Codex tools."""

    enabled: bool = True
    collector_endpoint: str = "http://127.0.0.1:6006/v1/traces"
    project_name: str = ""
    api_key: str | None = None
    max_tool_output_bytes: int = 128 * 1024

    def effective_project_name(self, model: str) -> str:
        if self.project_name.strip():
            return self.project_name.strip()
        slug = re.sub(r"[^a-z0-9]+", "-", model.strip().lower()).strip("-") or "unknown-model"
        return f"patchhound-experiments-codex-simple-{slug}"

    @classmethod
    def load(
        cls,
        *,
        env_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
        enabled: bool | None = None,
        endpoint: str | None = None,
        project_name: str | None = None,
    ) -> PhoenixConfig:
        path = (env_path or PROJECT_ROOT / ".env").expanduser().resolve()
        values = {key: value for key, value in dotenv_values(path).items() if isinstance(value, str)}
        values.update(dict(os.environ if environ is None else environ))
        loaded = cls(
            enabled=_bool_value(values, "PHOENIX_ENABLED", True),
            collector_endpoint=(
                values.get("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
                or "http://127.0.0.1:6006/v1/traces"
            ),
            project_name=values.get("PHOENIX_PROJECT_NAME", "").strip(),
            api_key=_optional(values.get("PHOENIX_API_KEY")),
            max_tool_output_bytes=_positive_int(
                values.get("PHOENIX_MAX_TOOL_OUTPUT_BYTES"),
                default=128 * 1024,
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


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


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
