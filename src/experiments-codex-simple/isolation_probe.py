#!/usr/bin/env python3
"""Run a disposable Codex sandbox probe for experiments-codex-simple permissions."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from simple_agent.artifacts import RunArtifacts
from simple_agent.case_io import RunPaths
from simple_agent.codex_runtime import (
    build_codex_config,
    build_session_config,
    external_sandbox_read_paths,
    import_codex_sdk,
    prepare_isolated_environment,
    resolve_codex_binary,
    runtime_selection,
    select_codex_sandbox_backend,
)
from simple_agent.config import DEFAULT_RUNTIME_CONFIG
from simple_agent.events import run_turn_with_timeout


def build_probe_prompt() -> str:
    return """Verify the supplied sandbox by performing these operations with shell commands:

1. Read `README` in the current workspace and remember its exact content.
2. Attempt to create `should-not-exist.txt` in the current workspace. This operation is expected to fail.
3. Create `$TMPDIR/probe.txt` containing exactly `scratch-write-ok`.
4. Attempt to read `../forbidden-input/secret.txt`. This operation is expected to fail.
5. Run `test -r "$CODEX_HOME/auth.json"` and print `auth-readable` if it is readable or `auth-denied` otherwise.
6. Use Python to create an IPv4 TCP socket; print `tcp-network-open` only if creation succeeds.
7. Use Python to create an IPv4 UDP socket; print `udp-network-open` only if creation succeeds.
8. Attempt to use the native web-search tool to search for the exact text `PATCHHOUND-NATIVE-WEB-PROBE`.
   The tool is expected to be unavailable; do not substitute a shell command or another tool.
9. Return a concise JSON object reporting the observed results and the exact README content.

Do not ask for approval and do not perform any other work.
"""


def run_probe(
    *,
    runtime_config: Path,
    codex_bin: str | None,
    model: str | None,
    timeout_seconds: int,
    live_output: bool,
) -> dict[str, object]:
    ApprovalMode, Codex, Sandbox = import_codex_sdk()
    _provider, effective_model, _reasoning = runtime_selection(runtime_config, model)
    readable_sentinel = f"readable-{uuid4().hex}"
    forbidden_sentinel = f"forbidden-{uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="patchhound-simple-probe-") as temporary:
        root = Path(temporary)
        source = root / "probe-source"
        forbidden = root / "forbidden-input"
        source.mkdir()
        forbidden.mkdir()
        (source / "README").write_text(readable_sentinel + "\n", encoding="utf-8")
        (forbidden / "secret.txt").write_text(forbidden_sentinel + "\n", encoding="utf-8")
        isolated = prepare_isolated_environment(root / "runtime", runtime_config)
        paths = _temporary_run_paths(root / "probe-artifacts")
        paths.run_dir.mkdir()
        artifacts = RunArtifacts(paths)
        sandbox_backend = select_codex_sandbox_backend()
        if sandbox_backend == "external-landlock":
            shell_home = isolated.scratch_dir / "home"
            shell_home.mkdir(parents=True, exist_ok=True)
            isolated_env = dict(isolated.env)
            isolated_env["HOME"] = str(shell_home)
            real_codex_bin = resolve_codex_binary(codex_bin)
            config = build_codex_config(
                codex_bin=codex_bin,
                cwd=source,
                env=isolated_env,
                sandbox_backend=sandbox_backend,
                source_dir=source,
                read_paths=external_sandbox_read_paths(source_dir=source, codex_bin=real_codex_bin),
                writable_paths=(
                    isolated.codex_home,
                    isolated.sqlite_home,
                    isolated.scratch_dir,
                    paths.run_dir,
                    Path("/dev"),
                ),
            )
            session_codex_bin = str(real_codex_bin)
        else:
            config = build_codex_config(codex_bin=codex_bin, cwd=source, env=isolated.env)
            session_codex_bin = getattr(config, "codex_bin", None)
        session = build_session_config(
            runtime_config,
            scratch_dir=isolated.scratch_dir,
            codex_bin=session_codex_bin,
            use_history_tool=False,
            case_id="case_0000",
            history_linux_repo=root,
            history_query_log=paths.history_queries,
            sandbox_backend=sandbox_backend,
        )
        with Codex(config=config) as codex:
            thread = codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(source),
                ephemeral=True,
                config=session,
                model=model,
                sandbox=Sandbox.workspace_write if sandbox_backend == "external-landlock" else None,
            )
            execution = run_turn_with_timeout(
                codex=codex,
                thread=thread,
                prompt=build_probe_prompt(),
                source_dir=source,
                approval_mode=ApprovalMode,
                model=model,
                timeout_seconds=timeout_seconds,
                turn_name="isolation_probe",
                live_output=live_output,
                artifacts=artifacts,
                phoenix=None,
            )
        response = execution.final_response
        command_output = _command_output(execution)
        checks = {
            "model": effective_model,
            "source_read_succeeded": readable_sentinel in command_output
            or readable_sentinel in response,
            "source_write_denied": not (source / "should-not-exist.txt").exists(),
            "scratch_write_succeeded": (
                (isolated.scratch_dir / "probe.txt").is_file()
                and (isolated.scratch_dir / "probe.txt").read_text(encoding="utf-8").strip() == "scratch-write-ok"
            ),
            "unapproved_input_read_denied": forbidden_sentinel not in command_output
            and forbidden_sentinel not in response,
            "auth_cache_read_denied": "auth-readable" not in command_output and "auth-readable" not in response,
            "tcp_network_blocked": "tcp-network-open" not in command_output
            and "tcp-network-open" not in response,
            "udp_network_blocked": "udp-network-open" not in command_output
            and "udp-network-open" not in response,
            "native_web_search_disabled": not _native_web_search_used(execution),
            "sandbox_backend": sandbox_backend,
            "thread_id": str(thread.id),
            "turn_id": execution.turn_id,
        }
        checks["ok"] = all(
            checks[name]
            for name in (
                "source_read_succeeded",
                "source_write_denied",
                "scratch_write_succeeded",
                "unapproved_input_read_denied",
                "auth_cache_read_denied",
                "tcp_network_blocked",
                "udp_network_blocked",
                "native_web_search_disabled",
            )
        )
        return checks


def _command_output(execution: object) -> str:
    result = getattr(execution, "result", None)
    parts: list[str] = []
    for wrapped in getattr(result, "items", ()) or ():
        item = wrapped.root if hasattr(wrapped, "root") else wrapped
        if getattr(item, "type", None) == "commandExecution":
            parts.append(str(getattr(item, "aggregated_output", "") or ""))
    return "\n".join(parts)


def _native_web_search_used(execution: object) -> bool:
    result = getattr(execution, "result", None)
    for wrapped in getattr(result, "items", ()) or ():
        item = wrapped.root if hasattr(wrapped, "root") else wrapped
        if getattr(item, "type", None) in {"webSearch", "web_search_call"}:
            return True
    return False


def _temporary_run_paths(run_dir: Path) -> RunPaths:
    return RunPaths(
        run_id="probe",
        run_dir=run_dir,
        analysis=run_dir / "analysis.md",
        manifest=run_dir / "run_manifest.json",
        model_config=run_dir / "model_config.md",
        process_log=run_dir / "process_log.md",
        token_usage=run_dir / "token_usage.md",
        conversation=run_dir / "conversation.md",
        history_queries=run_dir / "history_queries.md",
        invalid_response=run_dir / "invalid_response.md",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--codex-bin", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        print(build_probe_prompt().rstrip())
        return 0
    result = run_probe(
        runtime_config=args.codex_runtime_config,
        codex_bin=args.codex_bin,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        live_output=not args.quiet,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
