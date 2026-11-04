from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from omp_agent.config import EXPERIMENT_ROOT
from omp_agent.isolation import (
    IsolationError,
    build_bwrap_command,
    create_layout,
    run_isolation_probe,
)


def test_outer_sandbox_mounts_and_sanitizes_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    root = tmp_path / "sandbox"
    source.mkdir()
    (source / "read.c").write_text("int x;\n", encoding="utf-8")
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "agent.db").write_bytes(b"")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    command = build_bwrap_command(
        layout=create_layout(root),
        source_dir=source,
        command=["/app/node_modules/.bin/bun", "/app/dist/sidecar/index.js"],
        auth_data_dir=auth,
    )
    joined = "\0".join(command)
    assert "--unshare-net" not in command
    assert f"--ro-bind\0{source.resolve()}\0/workspace/source" in joined
    assert f"--ro-bind\0{EXPERIMENT_ROOT.resolve()}\0/app" in joined
    assert "--dir\0/runtime/omp-agent\0--bind" in joined
    assert "must-not-leak" not in joined
    assert "OMP_DISABLE_PROJECT_CONFIG\x001" in joined
    assert "PATCHHOUND_AGENT_SHELL_BACKEND\x00bubblewrap" in joined


def test_history_project_mount_includes_only_tools_and_root_marker(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "read.c").write_text("int x;\n", encoding="utf-8")
    project = tmp_path / "history-project"
    tools = project / "tools"
    tools.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='probe'\n", encoding="utf-8")
    command = build_bwrap_command(
        layout=create_layout(tmp_path / "sandbox"),
        source_dir=source,
        command=["/bin/true"],
        history_project_root=tools,
    )
    joined = "\0".join(command)
    assert f"--ro-bind\0{tools.resolve()}\0/runtime/history-project/tools" in joined
    assert (
        f"--ro-bind\0{(project / 'pyproject.toml').resolve()}\0/runtime/history-project/pyproject.toml" in joined
    )
    assert f"--ro-bind\0{project.resolve()}\0/runtime/history-project" not in joined


def test_overlapping_mount_roots_are_rejected(tmp_path: Path) -> None:
    layout = create_layout(tmp_path / "sandbox")
    source = layout.root / "source"
    source.mkdir()
    with pytest.raises(IsolationError, match="overlap"):
        build_bwrap_command(layout=layout, source_dir=source, command=["/bin/true"])


def test_inner_shell_does_not_mount_server_runtime_or_app() -> None:
    wrapper = (EXPERIMENT_ROOT / "bin" / "bash").read_text(encoding="utf-8")
    assert "--ro-bind /workspace/source /workspace/source" in wrapper
    assert "--bind /scratch /scratch" in wrapper
    assert "--unshare-net" in wrapper
    assert "--bind /runtime" not in wrapper
    assert "--ro-bind /app" not in wrapper
    assert "landlock_shell.py" in wrapper


def test_invalid_agent_shell_backend_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "read.c").write_text("int x;\n", encoding="utf-8")
    with pytest.raises(IsolationError, match="unsupported Agent shell isolation backend"):
        build_bwrap_command(
            layout=create_layout(tmp_path / "sandbox"),
            source_dir=source,
            command=["/bin/true"],
            agent_shell_backend="none",
        )


def test_live_readonly_probe_when_user_namespaces_are_available() -> None:
    with tempfile.TemporaryDirectory(prefix="patchhound-isolation-test-source-") as source_raw:
        with tempfile.TemporaryDirectory(prefix="patchhound-isolation-test-runtime-") as runtime_raw:
            source = Path(source_raw)
            (source / "read.c").write_text("int probe;\n", encoding="utf-8")
            try:
                result = run_isolation_probe(source, create_layout(Path(runtime_raw)))
            except IsolationError as exc:
                if "No permissions to create new namespace" in str(exc) or "Operation not permitted" in str(exc):
                    pytest.skip(f"execution environment blocks user namespaces: {exc}")
                raise
            assert result.ok
            assert result.agent_shell_backend in {"bubblewrap", "landlock"}
            assert result.tcp_network_blocked
            assert result.udp_network_blocked
            assert not (source / "probe-write").exists()
            assert os.readlink("/proc/self/ns/net")
