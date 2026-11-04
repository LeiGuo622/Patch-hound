#!/usr/bin/env python3
"""Probe PatchHound history MCP over stdio and through a real Codex turn."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from expert_agent.artifacts import RunArtifacts, sha256_file, sha256_text
from expert_agent.case_io import RunPaths, load_case
from expert_agent.codex_runtime import (
    HISTORY_MCP_SERVER_NAME,
    HISTORY_MCP_TOOL_NAME,
    build_codex_config,
    build_session_config,
    check_history_tool,
    external_sandbox_read_paths,
    import_codex_sdk,
    prepare_isolated_environment,
    resolve_codex_binary,
    resolve_linux_repo,
    runtime_selection,
    select_codex_sandbox_backend,
)
from expert_agent.config import (
    DEFAULT_CASES_DIR,
    DEFAULT_HISTORY_SERVER,
    DEFAULT_METADATA_DIR,
    DEFAULT_RUNTIME_CONFIG,
    PROJECT_ROOT,
    PhoenixConfig,
)
from expert_agent.events import run_turn_with_timeout
from expert_agent.phoenix import PhoenixObserver


def run_stdio_probe(
    *,
    case_id: str,
    kind: str,
    file_path: str,
    symbol: str | None,
    max_events: int,
    linux_repo: Path,
    query_log: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    arguments = _query_arguments(kind, file_path, symbol, max_events)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "experiments-codex-expert-probe", "version": "0.1.0"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": HISTORY_MCP_TOOL_NAME, "arguments": arguments},
        },
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "PATCHHOUND_CASE_ID": case_id,
            "PATCHHOUND_PROJECT_ROOT": str(PROJECT_ROOT),
            "PATCHHOUND_METADATA_DIR": str(DEFAULT_METADATA_DIR.resolve()),
            "PATCHHOUND_LINUX_REPO": str(linux_repo.resolve()),
            "PATCHHOUND_HISTORY_QUERY_LOG": str(query_log.resolve()),
        }
    )
    payload = "\n".join(json.dumps(request) for request in requests) + "\n"
    completed = subprocess.run(
        [sys.executable, "-B", str(DEFAULT_HISTORY_SERVER.resolve())],
        input=payload,
        text=True,
        capture_output=True,
        env=environment,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"history MCP server exited with {completed.returncode}: {_one_line(completed.stderr, 500)}"
        )
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    initialized = _response(responses, 1)
    listed = _response(responses, 2)
    called = _response(responses, 3)
    tools = listed.get("result", {}).get("tools", [])
    call_result = called.get("result", {})
    content = call_result.get("content", [])
    returned_text = "\n".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )
    tool_names = [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]
    result = {
        "transport": "stdio",
        "server": initialized.get("result", {}).get("serverInfo", {}).get("name"),
        "tool_listed": HISTORY_MCP_TOOL_NAME in tool_names,
        "call_is_error": bool(call_result.get("isError", True)),
        "returned_bytes": len(returned_text.encode("utf-8")),
        "history_boundary_present": "Only target patch ancestors were searched." in returned_text,
        "query_log_written": query_log.is_file(),
    }
    result["ok"] = all(
        (
            result["server"] == HISTORY_MCP_SERVER_NAME,
            result["tool_listed"],
            not result["call_is_error"],
            result["returned_bytes"] > 0,
            result["history_boundary_present"],
            result["query_log_written"],
        )
    )
    return result


def run_codex_probe(
    *,
    case_id: str,
    cases_dir: Path,
    runtime_config: Path,
    codex_bin: str | None,
    model: str | None,
    kind: str,
    file_path: str,
    symbol: str | None,
    max_events: int,
    linux_repo: Path,
    timeout_seconds: int,
    live_output: bool,
    phoenix_config: PhoenixConfig,
) -> dict[str, Any]:
    case = load_case(case_id, cases_dir)
    provider, effective_model, reasoning = runtime_selection(runtime_config, model)
    query = _query_arguments(kind, file_path, symbol, max_events)
    prompt = (
        f"Call the MCP tool `{HISTORY_MCP_SERVER_NAME}.{HISTORY_MCP_TOOL_NAME}` exactly once with "
        f"these arguments:\n\n```json\n{json.dumps(query, indent=2, sort_keys=True)}\n```\n\n"
        "Do not run shell commands or call any other tool. After the MCP call completes, respond with "
        "`MCP_PROBE_OK` followed by one concise sentence stating whether historical context was returned."
    )
    ApprovalMode, Codex, Sandbox = import_codex_sdk()
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="patchhound-history-mcp-probe-") as temporary:
        root = Path(temporary)
        paths = _temporary_run_paths(root / "artifacts")
        paths.run_dir.mkdir()
        artifacts = RunArtifacts(paths)
        isolated = prepare_isolated_environment(root / "runtime", runtime_config)
        sandbox_backend = select_codex_sandbox_backend()
        if sandbox_backend == "external-landlock":
            # Keep this diagnostic on the same isolated backend as the main
            # workflow when the host disallows nested bwrap namespaces.
            shell_home = isolated.scratch_dir / "home"
            shell_home.mkdir(parents=True, exist_ok=True)
            isolated_env = dict(isolated.env)
            isolated_env["HOME"] = str(shell_home)
            real_codex_bin = resolve_codex_binary(codex_bin)
            config = build_codex_config(
                codex_bin=codex_bin,
                cwd=case.source_dir,
                env=isolated_env,
                sandbox_backend=sandbox_backend,
                source_dir=case.source_dir,
                read_paths=external_sandbox_read_paths(
                    source_dir=case.source_dir,
                    codex_bin=real_codex_bin,
                    history_linux_repo=linux_repo,
                    history_metadata_dir=DEFAULT_METADATA_DIR,
                ),
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
            config = build_codex_config(codex_bin=codex_bin, cwd=case.source_dir, env=isolated.env)
            session_codex_bin = getattr(config, "codex_bin", None)
        session = build_session_config(
            runtime_config,
            scratch_dir=isolated.scratch_dir,
            codex_bin=session_codex_bin,
            use_history_tool=True,
            case_id=case.case_id,
            history_linux_repo=linux_repo,
            history_query_log=paths.history_queries,
            sandbox_backend=sandbox_backend,
        )
        observer = PhoenixObserver.create(
            config=phoenix_config,
            case_id=case.case_id,
            run_id="history_mcp_probe",
            model=effective_model,
            commit_message=case.commit_message,
            diff=case.diff,
            prompt_sha256=sha256_text(prompt),
            runtime_config_sha256=sha256_file(runtime_config),
            history_tool_enabled=True,
            warn=warnings.append,
            model_provider=provider,
            reasoning_effort=reasoning,
        )
        if observer is not None:
            observer.record_checkpoint(
                "runtime.configuration",
                input_value={"case_id": case.case_id, "probe": "history_mcp"},
                output_value={
                    "provider": provider,
                    "model": effective_model,
                    "reasoning_effort": reasoning,
                    "history_tool_enabled": True,
                },
            )
            observer.record_checkpoint(
                "runtime.codex_environment",
                input_value={"source_dir": str(case.source_dir)},
                output_value={
                    "codex_home": "isolated",
                    "scratch_dir": str(isolated.scratch_dir),
                    "source_access": "read",
                    "scratch_access": "write",
                    "network_enabled": False,
                    "control_plane_network_enabled": True,
                    "sandbox_backend": sandbox_backend,
                },
            )
        thread_id = "unknown"
        turn_ids: list[str] = []
        try:
            with Codex(config=config) as codex:
                thread = codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(case.source_dir),
                    ephemeral=True,
                    config=session,
                    model=model,
                    sandbox=Sandbox.workspace_write if sandbox_backend == "external-landlock" else None,
                )
                thread_id = str(thread.id)
                if observer is not None:
                    observer.set_thread_id(thread_id)
                    observer.record_thread_start(thread_id)
                status_available = check_history_tool(codex, thread_id)
                if observer is not None:
                    observer.record_checkpoint(
                        "history.mcp.status",
                        input_value={
                            "server": HISTORY_MCP_SERVER_NAME,
                            "tool": HISTORY_MCP_TOOL_NAME,
                        },
                        output_value={"available": status_available},
                    )
                execution = run_turn_with_timeout(
                    codex=codex,
                    thread=thread,
                    prompt=prompt,
                    source_dir=case.source_dir,
                    approval_mode=ApprovalMode,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    turn_name="history_mcp_probe",
                    live_output=live_output,
                    artifacts=artifacts,
                    phoenix=observer,
                )
                turn_ids.append(execution.turn_id)
                artifacts.record_tokens("history_mcp_probe", execution.result)
                artifacts.write_logs()
            mcp_items = [
                _root(item)
                for item in getattr(execution.result, "items", ()) or ()
                if getattr(_root(item), "type", None) == "mcpToolCall"
            ]
            matching = [
                item
                for item in mcp_items
                if getattr(item, "server", None) == HISTORY_MCP_SERVER_NAME
                and getattr(item, "tool", None) == HISTORY_MCP_TOOL_NAME
            ]
            item = matching[0] if len(matching) == 1 else None
            serialized_result = json.dumps(_jsonable(getattr(item, "result", None)), sort_keys=True)
            result = {
                "transport": "codex_app_server",
                "status_available": status_available,
                "matching_call_count": len(matching),
                "tool_status": _enum(getattr(item, "status", "missing")),
                "call_is_error": '"isError": true' in serialized_result,
                "query_log_written": paths.history_queries.is_file(),
                "response_acknowledged": "MCP_PROBE_OK" in execution.final_response,
                "thread_id": thread_id,
                "turn_id": execution.turn_id,
                "phoenix_trace_id": observer.trace_id if observer is not None else None,
                "phoenix_warning_count": len(warnings),
                "sandbox_backend": sandbox_backend,
            }
            result["ok"] = all(
                (
                    result["status_available"],
                    result["matching_call_count"] == 1,
                    result["tool_status"].lower() in {"completed", "success", "succeeded", "ok"},
                    not result["call_is_error"],
                    result["query_log_written"],
                    result["response_acknowledged"],
                )
            )
            if observer is not None:
                for path in (paths.history_queries, paths.process_log, paths.token_usage):
                    if path.is_file():
                        observer.record_checkpoint(
                            f"artifact.{path.stem}",
                            input_value={"artifact": path.name},
                            output_value={
                                "path": str(path),
                                "bytes": path.stat().st_size,
                                "sha256": sha256_file(path),
                            },
                        )
                observer.complete(
                    output=execution.final_response,
                    thread_ids={"history_mcp_probe": thread_id},
                    turn_ids=turn_ids,
                )
            return result
        except BaseException as exc:
            if observer is not None:
                observer.fail(stage="history_mcp_probe", exc=exc)
            raise


def _query_arguments(
    kind: str,
    file_path: str,
    symbol: str | None,
    max_events: int,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "kind": kind,
        "file_path": file_path,
        "max_events": max_events,
        "analysis_stage": "patch security review",
        "reason": "Verify history MCP end-to-end availability.",
    }
    if symbol:
        arguments["symbol"] = symbol
    return arguments


def _response(responses: list[dict[str, Any]], request_id: int) -> dict[str, Any]:
    matches = [response for response in responses if response.get("id") == request_id]
    if len(matches) != 1:
        raise RuntimeError(f"history MCP returned {len(matches)} responses for request {request_id}")
    response = matches[0]
    if "error" in response:
        raise RuntimeError(f"history MCP protocol error: {response['error']}")
    return response


def _temporary_run_paths(run_dir: Path) -> RunPaths:
    return RunPaths(
        run_id="history_mcp_probe",
        run_dir=run_dir,
        analysis=run_dir / "analysis.md",
        manifest=run_dir / "run_manifest.json",
        model_config=run_dir / "model_config.md",
        process_log=run_dir / "process_log.md",
        token_usage=run_dir / "token_usage.md",
        conversation=run_dir / "conversation.md",
        history_queries=run_dir / "history_queries.md",
    )


def _root(value: Any) -> Any:
    return value.root if hasattr(value, "root") else value


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, mode="json", exclude_none=True)
    if hasattr(value, "root"):
        return _jsonable(value.root)
    return str(value)


def _one_line(value: str, max_length: int) -> str:
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= max_length else collapsed[: max_length - 3] + "..."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--kind", choices=("function_history", "symbol_history", "file_history"), default="function_history")
    parser.add_argument("--file-path", required=True)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--max-events", type=int, default=1)
    parser.add_argument("--codex-runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--codex-bin", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--history-linux-repo", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--direct-only", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--phoenix", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--phoenix-endpoint", default=None)
    parser.add_argument("--phoenix-project", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_case(args.case_id, args.cases_dir)
    linux_repo = resolve_linux_repo(args.history_linux_repo, args.env_file)
    with tempfile.TemporaryDirectory(prefix="patchhound-history-stdio-probe-") as temporary:
        stdio = run_stdio_probe(
            case_id=args.case_id,
            kind=args.kind,
            file_path=args.file_path,
            symbol=args.symbol,
            max_events=args.max_events,
            linux_repo=linux_repo,
            query_log=Path(temporary) / "history_queries.md",
            timeout_seconds=min(args.timeout_seconds, 60),
        )
    result: dict[str, Any] = {"stdio": stdio}
    if not args.direct_only:
        phoenix_config = PhoenixConfig.load(
            runtime_path=args.codex_runtime_config,
            env_path=args.env_file,
            enabled=args.phoenix,
            endpoint=args.phoenix_endpoint,
            project_name=args.phoenix_project,
        )
        result["codex"] = run_codex_probe(
            case_id=args.case_id,
            cases_dir=args.cases_dir,
            runtime_config=args.codex_runtime_config,
            codex_bin=args.codex_bin,
            model=args.model,
            kind=args.kind,
            file_path=args.file_path,
            symbol=args.symbol,
            max_events=args.max_events,
            linux_repo=linux_repo,
            timeout_seconds=args.timeout_seconds,
            live_output=not args.quiet,
            phoenix_config=phoenix_config,
        )
    result["ok"] = stdio["ok"] and (args.direct_only or result["codex"]["ok"])
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
