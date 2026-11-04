#!/usr/bin/env python3
"""MCP server exposing controlled PatchHound pre-patch history queries."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from .history_context import DEFAULT_LINUX_REPO, DEFAULT_METADATA_DIR, query_pre_patch_history
except ImportError:  # pragma: no cover - used when executed as src/tools/history_mcp_server.py.
    PROJECT_ROOT = Path(os.environ.get("PATCHHOUND_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
    SRC_DIR = PROJECT_ROOT
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from tools.history_context import DEFAULT_LINUX_REPO, DEFAULT_METADATA_DIR, query_pre_patch_history  # noqa: E402


SERVER_NAME = "patchhound_history"
TOOL_NAME = "query_pre_patch_history"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
        except Exception as exc:  # noqa: BLE001 - keep the MCP process alive on malformed input.
            response = {
                "jsonrpc": "2.0",
                "id": _request_id_from_raw(line),
                "error": {"code": -32603, "message": str(exc)},
            }
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _result(
            request_id,
            {
                # MCP initialization negotiates a version; echoing an
                # arbitrary client version is invalid when it is newer than
                # the server's implementation. OpenCode 1.18.x currently
                # advertises 2025-11-25, while this server implements the
                # stable 2025-06-18 surface, so always select our supported
                # version here.
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                "instructions": (
                    "Use query_pre_patch_history for concrete PatchHound pre-patch history "
                    "questions about functions, symbols, or files touched by the patch."
                ),
            },
        )

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        # Omit nextCursor when pagination is complete. MCP clients (including
        # OpenCode's strict validator) treat null as an invalid value because
        # the field is optional rather than nullable.
        return _result(request_id, {"tools": [_tool_definition()]})

    if method == "tools/call":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if params.get("name") != TOOL_NAME:
            return _tool_error(request_id, f"unknown tool: {params.get('name')}")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        return _call_history_tool(request_id, arguments)

    if method == "resources/list":
        return _result(request_id, {"resources": []})

    if method == "resources/templates/list":
        return _result(request_id, {"resourceTemplates": []})

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _tool_definition() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Search sanitized historical context before the target patch. "
            "Use this for prior changes to functions, symbols, or files touched by the patch. "
            "The tool only returns pre-patch history and redacts identifying metadata."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["function_history", "symbol_history", "file_history"],
                    "description": "History query type.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Repository-relative file path visible in the case source tree.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Function or symbol name for function_history and symbol_history.",
                },
                "max_events": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Maximum number of historical events to return.",
                },
                "analysis_stage": {
                    "type": "string",
                    "description": "Optional stage label, for example Stage 2 execution flow.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional concise analysis question this history query should answer.",
                },
            },
            "required": ["kind", "file_path"],
            "additionalProperties": False,
        },
    }


def _call_history_tool(request_id: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        case_id = _required_env("PATCHHOUND_CASE_ID")
        result = query_pre_patch_history(
            case_id,
            kind=str(arguments.get("kind", "")),
            file_path=str(arguments.get("file_path", "")),
            symbol=_optional_string(arguments.get("symbol")),
            max_events=_optional_int(arguments.get("max_events"), 5),
            linux_repo=Path(os.environ.get("PATCHHOUND_LINUX_REPO", str(DEFAULT_LINUX_REPO))),
            metadata_dir=Path(os.environ.get("PATCHHOUND_METADATA_DIR", str(DEFAULT_METADATA_DIR))),
            record_query=True,
            query_log_path=_optional_path(os.environ.get("PATCHHOUND_HISTORY_QUERY_LOG")),
            query_note=_query_note(arguments),
        )
    except Exception as exc:  # noqa: BLE001 - tool failures should be visible to the agent as tool output.
        return _tool_error(request_id, str(exc))

    return _result(
        request_id,
        {
            "content": [{"type": "text", "text": result.text}],
            "isError": False,
        },
    )


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_error(request_id: Any, message: str) -> dict[str, Any]:
    return _result(
        request_id,
        {
            "content": [{"type": "text", "text": f"PatchHound history tool error: {message}"}],
            "isError": True,
        },
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _optional_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value)


def _query_note(arguments: dict[str, Any]) -> str | None:
    parts: list[str] = []
    stage = _optional_string(arguments.get("analysis_stage"))
    reason = _optional_string(arguments.get("reason"))
    if stage is not None:
        parts.append(f"stage={stage}")
    if reason is not None:
        parts.append(f"reason={reason}")
    return "; ".join(parts) if parts else None


def _request_id_from_raw(raw: str) -> Any:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed.get("id")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
