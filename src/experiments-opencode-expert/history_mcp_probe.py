#!/usr/bin/env python3
"""Probe direct and OpenCode-registered PatchHound history MCP transport without a model call."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from opencode_expert_agent.case_io import load_case
from opencode_expert_agent.config import DEFAULT_CASES_DIR, DEFAULT_METADATA_DIR, RuntimeConfig, resolve_linux_repo
from opencode_expert_agent.history_mcp import EXPOSED_TOOL_NAME, HistoryMcpBroker, direct_probe
from opencode_expert_agent.isolation import build_bwrap_command, create_layout
from opencode_expert_agent.runtime import SidecarRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id", nargs="?", default="case_0001")
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--linux-repo", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--file-path", required=True)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--direct-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    linux_repo = resolve_linux_repo(args.linux_repo, args.env_file)
    arguments = {
        "kind": "function_history" if args.symbol else "file_history",
        "file_path": args.file_path,
        "max_events": 1,
        "analysis_stage": "patch security review",
        "reason": "P4 history MCP probe",
    }
    if args.symbol:
        arguments["symbol"] = args.symbol
    direct = direct_probe(
        case_id=args.case_id,
        linux_repo=linux_repo,
        metadata_dir=args.metadata_dir,
        call_arguments=arguments,
    )
    output: dict[str, object] = {
        "direct": {
            "server": direct.definition.server_name,
            "tool": direct.definition.tool_name,
            "schema_sha256": direct.definition.schema_sha256,
            "call_succeeded": direct.call_succeeded,
            "response_is_error": direct.response_is_error,
            "response_bytes": direct.response_bytes,
        }
    }
    if args.direct_only:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if direct.call_succeeded else 1
    config = RuntimeConfig.load()
    source = load_case(args.case_id, args.cases_dir).source_dir
    with tempfile.TemporaryDirectory(prefix="patchhound-opencode-mcp-probe-") as raw:
        layout = create_layout(Path(raw))
        with HistoryMcpBroker(
            socket_path=layout.mcp_socket,
            case_id=args.case_id,
            linux_repo=linux_repo,
            metadata_dir=args.metadata_dir,
            query_log=layout.history_log,
        ) as broker:
            command = build_bwrap_command(
                layout=layout,
                source_dir=source,
                command=["/usr/bin/node", "/app/dist/sidecar/index.js"],
            )
            with SidecarRuntime(
                command,
                message_bytes=config.limits.message_bytes,
                stderr_bytes=config.limits.stderr_bytes,
                event_output_bytes=config.limits.event_output_bytes,
                total_event_bytes=config.limits.total_event_bytes,
                dispose_grace_seconds=config.dispose_grace_seconds,
                host_cwd=Path.cwd(),
            ) as runtime:
                ready = runtime.initialize(
                    {
                        "case_id": args.case_id,
                        "run_id": "probe",
                        "model_selector": config.model_selector,
                        "logical_model": config.logical_model,
                        "reasoning_effort": config.reasoning_effort,
                        "expected_version": config.sdk_version,
                        "server_ready_timeout_seconds": config.server_ready_timeout_seconds,
                        "message_bytes": config.limits.message_bytes,
                        "tool_output_bytes": config.limits.tool_output_bytes,
                        "enabled_tools": list(config.enabled_tools),
                        "disabled_tools": list(config.disabled_tools),
                        "history_enabled": True,
                        "mcp_command": [
                            "/usr/bin/node",
                            "/app/dist/sidecar/mcp_proxy.js",
                            "/runtime/history-mcp.sock",
                        ],
                        "mcp_schema_sha256": direct.definition.schema_sha256,
                        "mcp_timeout_ms": 30_000,
                    },
                    timeout=30,
                )
                session, mcp = runtime.start_session({}, timeout=30)
                broker.check()
                effective = session.payload.get("effective_tools")
                output["opencode"] = {
                    "server_version": ready.payload.get("serverVersion"),
                    "session_id": session.session_id,
                    "mcp_status": mcp.payload,
                    "effective_tools": effective,
                    "tools_list_requests": broker.method_counts["tools/list"],
                }
                ok = (
                    direct.call_succeeded
                    and mcp.payload.get("status") == "connected"
                    and isinstance(effective, list)
                    and EXPOSED_TOOL_NAME in effective
                    and broker.method_counts["tools/list"] >= 1
                )
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
