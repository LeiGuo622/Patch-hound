"""Codex SDK bootstrap, isolated environment, permissions, and history MCP."""

from __future__ import annotations

import copy
import ctypes
import importlib
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .config import (
    DEFAULT_HISTORY_SERVER,
    DEFAULT_LINUX_REPO,
    DEFAULT_METADATA_DIR,
    EXPERIMENT_ROOT,
    PROJECT_ROOT,
)

CODEX_AUTH_FILES = ("auth.json",)
CODEX_CLIENT_NAME = "codex_cli_rs"
CODEX_CONFIG_OVERRIDES = ('web_search="disabled"',)
HISTORY_MCP_SERVER_NAME = "patchhound_history"
HISTORY_MCP_TOOL_NAME = "query_pre_patch_history"
SANITIZED_ENV_KEYS = (
    "LINUX_REPO_PATH",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
    "DEEPSEEK_API_KEY",
    "PATCHHOUND_DEEPSEEK_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "PHOENIX_ENABLED",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_PROJECT_NAME",
    "PHOENIX_API_KEY",
    "PATCHHOUND_CASE_ID",
    "PATCHHOUND_HISTORY_QUERY_LOG",
    "PATCHHOUND_LINUX_REPO",
    "PATCHHOUND_METADATA_DIR",
    "PATCHHOUND_PROJECT_ROOT",
)


@dataclass(frozen=True)
class IsolatedEnvironment:
    temp_root: Path
    codex_home: Path
    sqlite_home: Path
    scratch_dir: Path
    env: dict[str, str]


def load_runtime_config(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Codex runtime config must be a TOML table: {path}")
    default_permissions = data.get("default_permissions")
    if not isinstance(default_permissions, str) or not default_permissions:
        raise ValueError("Codex runtime config must set default_permissions")
    return data


def runtime_selection(path: Path, model_override: str | None = None) -> tuple[str, str, str]:
    config = load_runtime_config(path)
    provider = str(config.get("model_provider") or "unknown")
    model = str(model_override or config.get("model") or "unknown")
    reasoning = str(config.get("model_reasoning_effort") or "unknown")
    return provider, model, reasoning


def prepare_isolated_environment(temp_root: Path, runtime_config: Path) -> IsolatedEnvironment:
    temp_root = temp_root.expanduser().resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    codex_home = temp_root / "codex-home"
    sqlite_home = temp_root / "codex-sqlite"
    scratch_dir = temp_root / "scratch"
    codex_home.mkdir()
    sqlite_home.mkdir()
    scratch_dir.mkdir()

    runtime = load_runtime_config(runtime_config)
    provider_env_key = runtime_provider_env_key(runtime)
    provider_env_value = resolve_provider_credential(provider_env_key) if provider_env_key else None
    if provider_env_key is None:
        source_home = source_codex_home()
        copied = 0
        for name in CODEX_AUTH_FILES:
            source = source_home / name
            if source.is_file():
                shutil.copy2(source, codex_home / name)
                copied += 1
        if copied == 0:
            raise FileNotFoundError(f"Codex login cache not found in {source_home}; run `codex login` first")

    shutil.copy2(runtime_config.expanduser().resolve(), codex_home / "config.toml")

    env = os.environ.copy()
    for key in SANITIZED_ENV_KEYS:
        env.pop(key, None)
    env["CODEX_HOME"] = str(codex_home)
    env["CODEX_SQLITE_HOME"] = str(sqlite_home)
    env["TMPDIR"] = str(scratch_dir)
    env["TMP"] = str(scratch_dir)
    env["TEMP"] = str(scratch_dir)
    if provider_env_key and provider_env_value:
        env[provider_env_key] = provider_env_value
    return IsolatedEnvironment(temp_root, codex_home, sqlite_home, scratch_dir, env)


def runtime_provider_env_key(config: dict[str, Any]) -> str | None:
    provider_name = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(provider_name, str) or not isinstance(providers, dict):
        return None
    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        return None
    env_key = provider.get("env_key")
    return env_key if isinstance(env_key, str) and env_key else None


def resolve_provider_credential(env_key: str) -> str:
    direct = os.environ.get(env_key, "").strip()
    dotenv = dotenv_values(PROJECT_ROOT / ".env").get(env_key)
    value = direct or (dotenv.strip() if isinstance(dotenv, str) else "")
    if not value:
        raise RuntimeError(f"{env_key} is required by the selected model provider")
    if any(char in value for char in "\r\n\x00"):
        raise RuntimeError(f"{env_key} contains invalid characters")
    return value


def source_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def resolve_linux_repo(explicit: Path | None = None, env_path: Path | None = None) -> Path:
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


def ensure_codex_sdk() -> Path:
    """Locate the separately installed, pinned Codex Python SDK."""
    try:
        sdk = importlib.import_module("openai_codex")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Codex SDK is not installed; run uv pip install -r requirements-codex.txt"
        ) from exc
    return Path(sdk.__file__).resolve().parent


def import_codex_sdk() -> tuple[Any, Any, Any]:
    ensure_codex_sdk()
    try:
        from openai_codex import ApprovalMode, Codex, Sandbox
    except ModuleNotFoundError as exc:
        raise RuntimeError("the installed Codex Python SDK is not importable") from exc
    return ApprovalMode, Codex, Sandbox


def build_codex_config(
    *,
    codex_bin: str | None,
    cwd: Path,
    env: dict[str, str],
    runtime_factory: Callable[[], Any] | None = None,
    sandbox_backend: str = "bubblewrap",
    source_dir: Path | None = None,
    read_paths: tuple[Path, ...] = (),
    writable_paths: tuple[Path, ...] = (),
) -> Any:
    if sandbox_backend not in {"bubblewrap", "external-landlock"}:
        raise ValueError(f"unsupported Codex sandbox backend: {sandbox_backend}")
    if sandbox_backend == "external-landlock":
        if source_dir is None:
            raise ValueError("source_dir is required for the external Landlock backend")
        real_bin = resolve_codex_binary(codex_bin)
        wrapper = EXPERIMENT_ROOT / "bin" / "landlock_codex.py"
        if not wrapper.is_file():
            raise FileNotFoundError(wrapper)
        _prepare_external_runtime_config(Path(env["CODEX_HOME"]) / "config.toml")
        config = _local_codex_config(real_bin)
        config.launch_args_override = (
            sys.executable,
            str(wrapper),
            "app-server",
            "--listen",
            "stdio://",
        )
        isolated_env = dict(env)
        isolated_env["PATCHHOUND_REAL_CODEX_BIN"] = str(real_bin)
        isolated_env["PATCHHOUND_LANDLOCK_READ_PATHS"] = os.pathsep.join(
            str(path.expanduser().resolve()) for path in read_paths
        )
        isolated_env["PATCHHOUND_LANDLOCK_WRITE_PATHS"] = os.pathsep.join(
            str(path.expanduser().resolve()) for path in writable_paths
        )
        env = isolated_env
    elif codex_bin is not None:
        config = _local_codex_config(codex_bin)
    elif runtime_factory is None and shutil.which("codex"):
        config = _local_codex_config("codex")
    else:
        config = runtime_factory() if runtime_factory is not None else _default_codex_config()
    # Native Responses web search runs on the control plane and is therefore
    # not constrained by the shell sandbox's network policy.  Disable it at
    # app-server launch as well as in the per-thread configuration below.
    config.config_overrides = CODEX_CONFIG_OVERRIDES
    config.cwd = str(cwd.resolve())
    config.env = env
    config.client_name = CODEX_CLIENT_NAME
    config.client_title = "PatchHound Experiments Codex Expert"
    config.client_version = "0.1.0"
    return config


def build_session_config(
    runtime_config: Path,
    *,
    scratch_dir: Path,
    codex_bin: str | None,
    use_history_tool: bool,
    case_id: str,
    history_linux_repo: Path,
    history_query_log: Path,
    python_bin: Path | None = None,
    sandbox_backend: str = "bubblewrap",
) -> dict[str, Any]:
    config = copy.deepcopy(load_runtime_config(runtime_config))
    config["web_search"] = "disabled"
    provider_env_key = runtime_provider_env_key(config)
    if sandbox_backend == "bubblewrap":
        profile = _permission_profile(config)
        filesystem = profile.setdefault("filesystem", {})
        if not isinstance(filesystem, dict):
            raise ValueError("permission profile filesystem must be a table")
        workspace = filesystem.setdefault(":workspace_roots", {})
        if not isinstance(workspace, dict):
            raise ValueError("permission profile :workspace_roots must be a table")
        workspace["."] = "read"
        filesystem[str(scratch_dir.resolve())] = "write"
        if codex_bin:
            for root in codex_runtime_read_roots(codex_bin):
                filesystem[str(root.resolve())] = "read"

        network = profile.setdefault("network", {})
        if not isinstance(network, dict):
            raise ValueError("permission profile network must be a table")
        network["enabled"] = False
    elif sandbox_backend == "external-landlock":
        # Permission profiles use Codex's direct runtime enforcement and are
        # intentionally incompatible with the legacy Landlock backend.  The
        # outer wrapper is the authoritative pathname boundary here, so select
        # the older workspace-write policy solely to make Codex install its
        # per-command legacy Landlock/seccomp network filter.  The outer
        # ruleset remains stricter and keeps the source tree read-only.
        config.pop("default_permissions", None)
        config.pop("permissions", None)
        config["sandbox_mode"] = "workspace-write"
        config["sandbox_workspace_write"] = {
            "writable_roots": [str(scratch_dir.resolve())],
            "network_access": False,
        }
        features = config.setdefault("features", {})
        if not isinstance(features, dict):
            raise ValueError("features must be a table")
        features["use_legacy_landlock"] = True
        # The app-server needs CODEX_HOME for authentication, but generated
        # shell commands must not inherit the copied auth cache location.
        config["shell_environment_policy"] = {
            "inherit": "all",
            "exclude": [
                "CODEX_HOME",
                "PATCHHOUND_LANDLOCK_*",
                "PATCHHOUND_REAL_CODEX_BIN",
                *([provider_env_key] if provider_env_key else []),
            ],
            "set": {"CODEX_HOME": "/nonexistent"},
        }
    else:
        raise ValueError(f"unsupported Codex sandbox backend: {sandbox_backend}")

    if use_history_tool:
        mcp_servers = config.setdefault("mcp_servers", {})
        if not isinstance(mcp_servers, dict):
            raise ValueError("Codex runtime config mcp_servers must be a table")
        mcp_servers[HISTORY_MCP_SERVER_NAME] = {
            "command": str((python_bin or Path(sys.executable)).resolve()),
            "args": ["-B", str(DEFAULT_HISTORY_SERVER.resolve())],
            "env": {
                "PATCHHOUND_CASE_ID": case_id,
                "PATCHHOUND_PROJECT_ROOT": str(PROJECT_ROOT),
                "PATCHHOUND_METADATA_DIR": str(DEFAULT_METADATA_DIR.resolve()),
                "PATCHHOUND_LINUX_REPO": str(history_linux_repo.resolve()),
                "PATCHHOUND_HISTORY_QUERY_LOG": str(history_query_log.resolve()),
            },
            "default_tools_approval_mode": "approve",
            "required": True,
        }
    return config


def _prepare_external_runtime_config(path: Path) -> None:
    """Convert a copied profile config to the legacy sandbox dialect.

    Codex rejects permission profiles together with ``use_legacy_landlock``.
    Only the disposable CODEX_HOME copy is rewritten; repository configs stay
    in the current permission-profile format for the normal bwrap backend.
    """

    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)
    output: list[str] = []
    skipping_permissions = False
    inserted_sandbox_mode = False
    inserted_web_search = False
    for line in lines:
        stripped = line.strip()
        if not skipping_permissions and stripped.startswith("web_search") and "=" in stripped:
            if not inserted_web_search:
                output.append('web_search = "disabled"\n')
                inserted_web_search = True
            continue
        if not skipping_permissions and stripped.startswith("default_permissions") and "=" in stripped:
            continue
        if stripped.startswith("["):
            section = stripped.lstrip("[").split("]", 1)[0].strip()
            skipping_permissions = section == "permissions" or section.startswith("permissions.")
            if skipping_permissions:
                continue
            if not inserted_web_search:
                output.append('web_search = "disabled"\n')
                inserted_web_search = True
            if not inserted_sandbox_mode:
                output.append('sandbox_mode = "workspace-write"\n')
                inserted_sandbox_mode = True
        if not skipping_permissions:
            output.append(line)
    if not inserted_sandbox_mode:
        output.append('sandbox_mode = "workspace-write"\n')
    if not inserted_web_search:
        output.append('web_search = "disabled"\n')
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    output.extend(("\n[sandbox_workspace_write]\n", "network_access = false\n"))
    path.write_text("".join(output), encoding="utf-8")


def check_history_tool(codex: Any, thread_id: str) -> bool:
    request_raw = _raw_request_method(codex)
    if request_raw is None:
        return False
    response = request_raw("mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"})
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise RuntimeError("Codex mcpServerStatus/list returned an invalid response")
    server = next(
        (
            item
            for item in response["data"]
            if isinstance(item, dict) and item.get("name") == HISTORY_MCP_SERVER_NAME
        ),
        None,
    )
    if server is None:
        raise RuntimeError(f"Codex did not report MCP server `{HISTORY_MCP_SERVER_NAME}`")
    tools = server.get("tools")
    if not isinstance(tools, dict) or HISTORY_MCP_TOOL_NAME not in tools:
        available = sorted(str(name) for name in tools) if isinstance(tools, dict) else []
        raise RuntimeError(
            f"MCP server `{HISTORY_MCP_SERVER_NAME}` did not expose `{HISTORY_MCP_TOOL_NAME}`; "
            f"available={available}"
        )
    return True


def codex_runtime_read_roots(codex_bin: str) -> tuple[Path, ...]:
    raw = Path(codex_bin).expanduser()
    roots = [raw.parent]
    try:
        resolved = raw.resolve(strict=False)
    except OSError:
        resolved = raw
    roots.append(resolved.parent)
    for parent in resolved.parents:
        if parent.name == "codex" and parent.parent.name == "@openai":
            roots.append(parent)
            break
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return tuple(deduped)


def resolve_codex_binary(codex_bin: str | None) -> Path:
    """Resolve the executable that the external wrapper will eventually exec."""
    if codex_bin is not None:
        return Path(_resolve_codex_bin(codex_bin)).expanduser().resolve()
    selected = shutil.which("codex")
    if selected:
        return Path(selected).expanduser().resolve()
    try:
        from codex_cli_bin import bundled_codex_path
    except (ImportError, AttributeError) as exc:
        raise FileNotFoundError("unable to locate the Codex executable for the Landlock wrapper") from exc
    return Path(bundled_codex_path()).expanduser().resolve()


def external_sandbox_read_paths(
    *,
    source_dir: Path,
    codex_bin: Path,
    history_linux_repo: Path | None = None,
    history_metadata_dir: Path | None = None,
) -> tuple[Path, ...]:
    """Return host paths needed by Codex and the optional history MCP child."""
    candidates: list[Path] = [
        Path("/usr"),
        Path("/etc"),
        Path("/sys"),
        Path("/run"),
        source_dir,
        PROJECT_ROOT / "src",
        PROJECT_ROOT / "tools",
        PROJECT_ROOT / "pyproject.toml",
        *codex_runtime_read_roots(str(codex_bin)),
    ]
    if history_linux_repo is not None:
        candidates.append(history_linux_repo)
    if history_metadata_dir is not None:
        candidates.append(history_metadata_dir)
    # The MCP process is launched with this interpreter.  Include its runtime
    # and import roots so Landlock does not turn a valid tool call into an
    # opaque interpreter import failure.
    candidates.extend((Path(sys.executable).resolve().parent, Path(sys.prefix).resolve()))
    if sys.base_prefix:
        candidates.append(Path(sys.base_prefix).resolve())
    candidates.extend(Path(item).expanduser().resolve() for item in sys.path if item)
    result: list[Path] = []
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if not path.exists() or path in result:
            continue
        result.append(path)
    return tuple(result)


@lru_cache(maxsize=1)
def select_codex_sandbox_backend() -> str:
    """Use Codex bwrap when possible, otherwise select the Landlock fallback."""
    bwrap = shutil.which("bwrap")
    if bwrap:
        command = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-user-try",
            "--proc",
            "/proc",
            "--ro-bind",
            "/",
            "/",
            "/usr/bin/true",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0:
            return "bubblewrap"
    if _landlock_supported():
        return "external-landlock"
    detail = "bubblewrap user namespaces are unavailable and Landlock is unavailable"
    if bwrap:
        detail += f" (bwrap={bwrap})"
    raise RuntimeError(detail)


def _landlock_supported() -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    result = int(libc.syscall(444, 0, 0, 1))
    return result >= 1


def _permission_profile(config: dict[str, Any]) -> dict[str, Any]:
    name = config.get("default_permissions")
    if not isinstance(name, str) or not name:
        raise ValueError("Codex runtime config must set default_permissions")
    permissions = config.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("Codex runtime config permissions must be a table")
    profile = permissions.setdefault(name, {})
    if not isinstance(profile, dict):
        raise ValueError(f"permissions.{name} must be a table")
    return profile


def _default_codex_config() -> Any:
    ensure_codex_sdk()
    from openai_codex import CodexConfig

    return CodexConfig()


def _resolve_codex_bin(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if resolved:
        return resolved
    raise FileNotFoundError(f"Codex CLI executable not found: {value}")


def _local_codex_config(codex_bin: str) -> Any:
    ensure_codex_sdk()
    from openai_codex import CodexConfig

    return CodexConfig(codex_bin=_resolve_codex_bin(codex_bin))


def _raw_request_method(codex: Any) -> Callable[[str, dict[str, Any] | None], Any] | None:
    client = getattr(codex, "_client", None)
    request = getattr(client, "_request_raw", None)
    if callable(request):
        return request
    request = getattr(codex, "_request_raw", None)
    return request if callable(request) else None


__all__ = [
    "CODEX_CLIENT_NAME",
    "HISTORY_MCP_SERVER_NAME",
    "HISTORY_MCP_TOOL_NAME",
    "IsolatedEnvironment",
    "build_codex_config",
    "build_session_config",
    "check_history_tool",
    "codex_runtime_read_roots",
    "external_sandbox_read_paths",
    "ensure_codex_sdk",
    "import_codex_sdk",
    "load_runtime_config",
    "prepare_isolated_environment",
    "resolve_codex_binary",
    "resolve_linux_repo",
    "resolve_provider_credential",
    "runtime_provider_env_key",
    "runtime_selection",
    "select_codex_sandbox_backend",
]
