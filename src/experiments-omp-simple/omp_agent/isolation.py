"""OS isolation for the OMP server and its restricted Agent shell."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .config import EXPERIMENT_ROOT


class IsolationError(RuntimeError):
    """Raised when the required OS isolation boundary cannot be established."""


@dataclass(frozen=True)
class SandboxLayout:
    root: Path
    runtime: Path
    config: Path
    home: Path
    data: Path
    cache: Path
    state: Path
    scratch: Path
    scratch_home: Path
    scratch_tmp: Path
    mcp_socket: Path
    history_log: Path


@dataclass(frozen=True)
class IsolationProbeResult:
    source_read: bool
    source_write_blocked: bool
    source_rename_blocked: bool
    source_delete_blocked: bool
    scratch_write: bool
    runtime_unreadable_from_agent_shell: bool
    repository_unreadable_from_agent_shell: bool
    tcp_network_blocked: bool
    udp_network_blocked: bool
    network_namespace_shared: bool
    agent_shell_backend: str = "bubblewrap"

    @property
    def ok(self) -> bool:
        return all(
            (
                self.source_read,
                self.source_write_blocked,
                self.source_rename_blocked,
                self.source_delete_blocked,
                self.scratch_write,
                self.runtime_unreadable_from_agent_shell,
                self.repository_unreadable_from_agent_shell,
                self.tcp_network_blocked,
                self.udp_network_blocked,
            )
        )

    def as_dict(self) -> dict[str, bool | str]:
        return dict(vars(self))


@dataclass(frozen=True)
class NetworkProbeResult:
    """Result of a real TCP connectivity check from the restricted Agent shell."""

    target: str
    reachable: bool
    elapsed_ms: int
    error: str | None = None

    def as_dict(self) -> dict[str, bool | str | int | None]:
        return dict(vars(self))


def create_layout(root: Path) -> SandboxLayout:
    resolved = root.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    runtime = resolved / "runtime"
    config = resolved / "config-ro"
    home = runtime / "home"
    data = runtime / "data"
    cache = runtime / "cache"
    state = runtime / "state"
    scratch = resolved / "scratch"
    scratch_home = scratch / "home"
    scratch_tmp = scratch / "tmp"
    for path in (
        runtime,
        config,
        home,
        data,
        cache,
        state,
        scratch,
        scratch_home,
        scratch_tmp,
        scratch / "xdg-config",
        scratch / "xdg-cache",
        scratch / "xdg-data",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return SandboxLayout(
        root=resolved,
        runtime=runtime,
        config=config,
        home=home,
        data=data,
        cache=cache,
        state=state,
        scratch=scratch,
        scratch_home=scratch_home,
        scratch_tmp=scratch_tmp,
        mcp_socket=runtime / "history-mcp.sock",
        history_log=runtime / "history_queries.md",
    )


def build_bwrap_command(
    *,
    layout: SandboxLayout,
    source_dir: Path,
    command: list[str],
    experiment_root: Path = EXPERIMENT_ROOT,
    auth_data_dir: Path | None = None,
    history_server: Path | None = None,
    history_linux_repo: Path | None = None,
    history_metadata_dir: Path | None = None,
    history_project_root: Path | None = None,
    agent_shell_backend: str = "bubblewrap",
    environment: Mapping[str, str] | None = None,
    agent_model_config: Path | None = None,
    provider_env_file: Path | None = None,
) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise IsolationError("bubblewrap executable was not found")
    source = source_dir.expanduser().resolve()
    experiment = experiment_root.expanduser().resolve()
    _validate_mounts(source, layout, experiment)
    if not (experiment / "bin" / "bash").is_file():
        raise IsolationError("sandboxed bash wrapper is missing")
    if agent_shell_backend not in {"bubblewrap", "landlock"}:
        raise IsolationError(f"unsupported Agent shell isolation backend: {agent_shell_backend}")
    if agent_shell_backend == "landlock" and not (experiment / "bin" / "landlock_shell.py").is_file():
        raise IsolationError("Landlock Agent shell helper is missing")
    args = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user-try",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--ro-bind",
        "/etc",
        "/etc",
        "--dir",
        "/run",
        "--dir",
        "/run/systemd",
        "--dir",
        "/run/systemd/resolve",
        "--ro-bind-try",
        "/run/systemd/resolve/stub-resolv.conf",
        "/run/systemd/resolve/stub-resolv.conf",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(experiment),
        "/app",
        "--ro-bind",
        str(source),
        "/workspace/source",
        "--bind",
        str(layout.runtime),
        "/runtime",
        "--ro-bind",
        str(layout.config),
        "/runtime/config",
        "--bind",
        str(layout.scratch),
        "/scratch",
    ]
    if auth_data_dir is not None:
        auth = auth_data_dir.expanduser().resolve()
        if not auth.is_dir():
            raise FileNotFoundError(auth)
        if auth.is_relative_to(source) or source.is_relative_to(auth):
            raise IsolationError("auth data directory overlaps source")
        if auth.is_relative_to(layout.root) or layout.root.is_relative_to(auth):
            raise IsolationError("auth data directory overlaps sandbox root")
        if auth.is_relative_to(experiment) or experiment.is_relative_to(auth):
            raise IsolationError("auth data directory overlaps experiment files")
        args.extend(["--dir", "/runtime/omp-agent", "--bind", str(auth), "/runtime/omp-agent"])
    elif agent_model_config is not None:
        args.append("--dir")
        args.append("/runtime/omp-agent")
    if agent_model_config is not None:
        model_config = agent_model_config.expanduser().resolve()
        if not model_config.is_file():
            raise FileNotFoundError(model_config)
        args.extend(["--ro-bind", str(model_config), "/runtime/omp-agent/models.yml"])
    if provider_env_file is not None:
        env_file = provider_env_file.expanduser().resolve()
        if not env_file.is_file():
            raise FileNotFoundError(env_file)
        args.extend(["--ro-bind", str(env_file), "/runtime/provider.env"])
    for name, path, target in (
        ("history server", history_server, "/runtime/history-tools/history_mcp_server.py"),
        ("history linux repository", history_linux_repo, "/runtime/history-linux"),
        ("history metadata", history_metadata_dir, "/runtime/history-metadata"),
        ("history project tools", history_project_root, "/runtime/history-project/tools"),
    ):
        if path is None:
            continue
        resolved = path.expanduser().resolve()
        if not resolved.exists() or (name != "history server" and not resolved.is_dir()) or (name == "history server" and not resolved.is_file()):
            raise FileNotFoundError(resolved)
        if resolved.is_relative_to(source) or source.is_relative_to(resolved):
            raise IsolationError(f"{name} overlaps source")
        if resolved.is_relative_to(layout.root) or layout.root.is_relative_to(resolved):
            raise IsolationError(f"{name} overlaps sandbox root")
        if resolved.is_relative_to(experiment) or experiment.is_relative_to(resolved):
            raise IsolationError(f"{name} overlaps experiment files")
        mount_flag = "--ro-bind"
        if name == "history server":
            args.extend(["--dir", "/runtime/history-tools"])
        elif name == "history project tools":
            args.extend(["--dir", "/runtime/history-project"])
        args.extend([mount_flag, str(resolved), target])
        if name == "history project tools":
            project_marker = resolved.parent / "pyproject.toml"
            if not project_marker.is_file() or project_marker.is_symlink():
                raise IsolationError(f"history project marker is missing or unsafe: {project_marker}")
            args.extend(["--ro-bind", str(project_marker), "/runtime/history-project/pyproject.toml"])
    env = {
        "HOME": "/runtime/home",
        "XDG_CONFIG_HOME": "/runtime/config",
        "XDG_DATA_HOME": "/runtime/data",
        "XDG_CACHE_HOME": "/runtime/cache",
        "XDG_STATE_HOME": "/runtime/state",
        "TMPDIR": "/runtime/cache",
        "PATH": "/app/node_modules/.bin:/usr/bin:/bin",
        "SHELL": "/app/bin/bash",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PI_CODING_AGENT_DIR": "/runtime/omp-agent",
        "OMP_CONFIG_DIR": "/runtime/config",
        "OMP_DISABLE_PROJECT_CONFIG": "1",
        "OMP_DISABLE_EXTERNAL_SKILLS": "1",
        "OMP_DISABLE_LSP_DOWNLOAD": "1",
        "OMP_DISABLE_AUTOUPDATE": "1",
        "OMP_CLIENT": "sdk",
        "PATCHHOUND_AGENT_SOURCE": "/workspace/source",
        "PATCHHOUND_AGENT_SCRATCH": "/scratch",
        "PATCHHOUND_AGENT_SHELL_BACKEND": agent_shell_backend,
    }
    if environment:
        for key, value in environment.items():
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise IsolationError("invalid environment override")
            env[key] = value
    args.append("--clearenv")
    for key, value in env.items():
        args.extend(["--setenv", key, value])
    args.extend(["--chdir", "/workspace/source"])
    if provider_env_file is not None:
        args.extend(
            [
                "/bin/sh",
                "-c",
                "set -a; . /runtime/provider.env; set +a; exec \"$@\"",
                "patchhound-provider-launcher",
                *command,
            ]
        )
    else:
        args.extend(command)
    return args


def run_isolation_probe(source_dir: Path, layout: SandboxLayout) -> IsolationProbeResult:
    secret = layout.runtime / "probe-secret"
    secret.write_text("not visible to agent shell", encoding="utf-8")
    readable = next((path for path in source_dir.rglob("*") if path.is_file() and not path.is_symlink()), None)
    if readable is None:
        raise IsolationError(f"source tree contains no regular file for the read probe: {source_dir}")
    relative = readable.relative_to(source_dir).as_posix()
    source_file = shlex.quote(f"/workspace/source/{relative}")
    host_net_namespace = shlex.quote(os.readlink("/proc/self/ns/net"))
    backend = select_agent_shell_backend(source_dir, layout)
    script = rf'''
set +e
head -c 1 {source_file} >/dev/null 2>&1; echo "source_read=$?"
touch /workspace/source/probe-write 2>/dev/null; echo "source_write=$?"
mv {source_file} /workspace/source/probe-renamed 2>/dev/null; echo "source_rename=$?"
rm {source_file} 2>/dev/null; echo "source_delete=$?"
touch /scratch/probe-write 2>/dev/null; echo "scratch_write=$?"
cat /runtime/probe-secret >/dev/null 2>&1; echo "runtime_read=$?"
cat /app/IMPLEMENTATION_PLAN.md >/dev/null 2>&1; echo "repository_read=$?"
/usr/bin/python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)' >/dev/null 2>&1; echo "tcp_socket=$?"
/usr/bin/python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM)' >/dev/null 2>&1; echo "udp_socket=$?"
test "$(readlink /proc/self/ns/net)" = {host_net_namespace}; echo "network_namespace=$?"
'''
    command = build_bwrap_command(
        layout=layout,
        source_dir=source_dir,
        command=["/app/bin/bash", "-c", script],
        agent_shell_backend=backend,
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise IsolationError(f"isolation probe failed: {result.stderr.strip()[:1000]}")
    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, separator, raw = line.partition("=")
        if separator and raw.isdigit():
            values[key] = int(raw)
    probe = IsolationProbeResult(
        source_read=values.get("source_read") == 0,
        source_write_blocked=values.get("source_write", 0) != 0,
        source_rename_blocked=values.get("source_rename", 0) != 0,
        source_delete_blocked=values.get("source_delete", 0) != 0,
        scratch_write=values.get("scratch_write") == 0,
        runtime_unreadable_from_agent_shell=values.get("runtime_read", 0) != 0,
        repository_unreadable_from_agent_shell=values.get("repository_read", 0) != 0,
        tcp_network_blocked=values.get("tcp_socket", 0) != 0,
        udp_network_blocked=values.get("udp_socket", 0) != 0,
        network_namespace_shared=values.get("network_namespace") == 0,
        agent_shell_backend=backend,
    )
    if not probe.ok:
        raise IsolationError(f"isolation probe did not satisfy the contract: {probe.as_dict()}")
    return probe


def run_network_connectivity_probe(
    source_dir: Path,
    layout: SandboxLayout,
    *,
    target_url: str,
    agent_shell_backend: str,
) -> NetworkProbeResult:
    """Attempt a real TCP connection from the restricted Agent shell."""
    parsed = urlsplit(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return NetworkProbeResult(target_url, False, 0, "target must be an http(s) URL")
    try:
        parsed.port
    except ValueError:
        return NetworkProbeResult(target_url, False, 0, "target port is invalid")
    script = """
import socket
import sys
from urllib.parse import urlsplit

target = urlsplit(sys.argv[1])
port = target.port or (443 if target.scheme == "https" else 80)
try:
    sock = socket.create_connection((target.hostname, port), timeout=5)
    sock.close()
    print("network_connectivity=1")
except Exception as exc:
    print("network_connectivity=0")
    print(f"network_error={type(exc).__name__}:{exc}")
"""
    command_script = f"exec /usr/bin/python3 -c {shlex.quote(script)} {shlex.quote(target_url)}"
    command = build_bwrap_command(
        layout=layout,
        source_dir=source_dir,
        command=["/app/bin/bash", "-c", command_script],
        agent_shell_backend=agent_shell_backend,
    )
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    elapsed_ms = int(max(0.0, time.monotonic() - started) * 1000)
    values = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    reachable = result.returncode == 0 and values.get("network_connectivity") == "1"
    error = None if reachable else (values.get("network_error") or "probe process failed")
    return NetworkProbeResult(target_url, reachable, elapsed_ms, error[:500] if error else None)


def select_agent_shell_backend(source_dir: Path, layout: SandboxLayout) -> str:
    """Choose a fail-closed Agent shell backend using harmless live probes."""
    failures: list[str] = []
    for backend in ("bubblewrap", "landlock"):
        command = build_bwrap_command(
            layout=layout,
            source_dir=source_dir,
            command=["/app/bin/bash", "-c", "true"],
            agent_shell_backend=backend,
        )
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0:
            return backend
        diagnostic = " ".join((result.stderr or result.stdout).split())[:500]
        failures.append(f"{backend}=exit {result.returncode}: {diagnostic or 'no diagnostic'}")
    raise IsolationError("no supported Agent shell isolation backend passed preflight: " + "; ".join(failures))


def _validate_mounts(source: Path, layout: SandboxLayout, experiment: Path) -> None:
    for name, path in (("source", source), ("sandbox root", layout.root), ("experiment", experiment)):
        if not path.is_absolute() or not path.exists():
            raise IsolationError(f"{name} must be an existing absolute path: {path}")
    pairs = ((source, layout.root), (source, experiment), (layout.root, experiment))
    for left, right in pairs:
        if left == right or left.is_relative_to(right) or right.is_relative_to(left):
            raise IsolationError(f"isolation mount paths overlap: {left} and {right}")
    if source.is_symlink() or layout.root.is_symlink() or experiment.is_symlink():
        raise IsolationError("isolation mount roots must not be symlinks")
    if os.name != "posix":
        raise IsolationError("bubblewrap isolation is supported only on POSIX Linux")
