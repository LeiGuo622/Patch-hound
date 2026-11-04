#!/usr/bin/env python3
"""Probe direct and OMP-registered history MCP transport without a model call."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from omp_agent.case_io import load_case
from omp_agent.config import (
    DEFAULT_CASES_DIR,
    DEFAULT_HISTORY_SERVER,
    DEFAULT_METADATA_DIR,
    PROJECT_ROOT,
    RuntimeConfig,
    resolve_linux_repo,
)
from omp_agent.history_mcp import EXPOSED_TOOL_NAME, direct_probe
from omp_agent.isolation import build_bwrap_command, create_layout
from omp_agent.runtime import SidecarRuntime


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
    with tempfile.TemporaryDirectory(prefix="patchhound-omp-mcp-probe-") as raw:
        layout = create_layout(Path(raw))
        command = build_bwrap_command(
            layout=layout,
            source_dir=source,
            history_server=DEFAULT_HISTORY_SERVER,
            history_linux_repo=linux_repo,
            history_metadata_dir=args.metadata_dir,
            history_project_root=PROJECT_ROOT / "tools",
            command=["/app/node_modules/.bin/bun", "/app/dist/sidecar/index.js"],
        )
        with SidecarRuntime(
            command,
            message_bytes=config.limits.message_bytes,
            stderr_bytes=config.limits.stderr_bytes,
            event_output_bytes=config.limits.event_output_bytes,
            total_event_bytes=config.limits.total_event_bytes,
            dispose_grace_seconds=config.dispose_grace_seconds,
            host_cwd=PROJECT_ROOT,
        ) as runtime:
            ready = runtime.initialize(
                {
                    "case_id": args.case_id,
                    "run_id": "probe",
                    "model_selector": config.model_selector,
                    "logical_model": config.logical_model,
                    "reasoning_effort": config.reasoning_effort,
                    "omp_sdk_version": config.omp_sdk_version,
                    "bun_min_version": config.bun_min_version,
                    "agent_dir": "/runtime/omp-agent",
                    "provider": config.provider,
                    "auth_required": False,
                    "sidecar_protocol_version": config.sidecar_protocol_version,
                    "message_bytes": config.limits.message_bytes,
                    "enabled_tools": list(config.enabled_tools),
                    "disabled_tools": list(config.disabled_tools),
                    "history_enabled": True,
                    "mcp_command": ["/usr/bin/python3", "-B", "/runtime/history-tools/history_mcp_server.py"],
                    "mcp_schema_sha256": direct.definition.schema_sha256,
                    "mcp_timeout_ms": config.mcp_timeout_ms,
                    "mcp_probe_arguments": arguments,
                    "project_root": "/runtime/history-project",
                    "metadata_dir": "/runtime/history-metadata",
                    "linux_repo": "/runtime/history-linux",
                    "query_log": "/runtime/history_queries.md",
                },
                timeout=config.server_ready_timeout_seconds,
            )
            session, mcp = runtime.start_session({}, timeout=config.server_ready_timeout_seconds)
            effective = session.payload.get("effective_tools")
            output["omp"] = {
                "sdk_version": ready.payload.get("sdkVersion"),
                "bun_version": ready.payload.get("bunVersion"),
                "session_id": session.session_id,
                "mcp_status": mcp.payload,
                "effective_tools": effective,
                "history_log_created": layout.history_log.is_file(),
            }
            ok = (
                direct.call_succeeded
                and mcp.payload.get("status") == "connected"
                and mcp.payload.get("call_probe") == "succeeded"
                and layout.history_log.is_file()
                and isinstance(effective, list)
                and EXPOSED_TOOL_NAME in effective
            )
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
