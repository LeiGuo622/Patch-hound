#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime

VALID = """# Patch Security Analysis

- Final Assessment: NO_NEW_ISSUES
- Confidence: HIGH

## Patch Summary

The fake runtime completed.

## New Security Issues

None.

## Limitations

This was a protocol test.
"""

mode = sys.argv[1] if len(sys.argv) > 1 else "success"
counter = 0
session_id = "fake-session"
prompt_count = 0


def emit(kind: str, payload: dict[str, object], request: str, *, turn: str | None = None) -> None:
    global counter
    counter += 1
    value: dict[str, object] = {
        "schema_version": 1,
        "type": kind,
        "id": f"fake-{counter}",
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": payload,
        "request_id": request,
    }
    if kind not in {"ready"}:
        value["session_id"] = session_id
    if turn is not None:
        value["turn_id"] = turn
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    request_id = request["id"]
    kind = request["type"]
    if mode == "crash":
        sys.stderr.write("fake sidecar crashed\n")
        sys.stderr.flush()
        raise SystemExit(17)
    if mode == "malformed":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        continue
    if kind == "initialize":
        emit(
            "ready",
            {
                "serverVersion": "18.0.11",
                "sdkVersion": "18.0.11",
                "cliVersion": "18.0.11",
                "configuredModel": "openai-codex/gpt-5.6-sol",
                "configuredReasoning": "high",
                "permissionPolicy": {"*": "deny", "read": "allow"},
            },
            request_id,
        )
    elif kind == "start_session":
        emit("session_started", {"effective_tools": ["read"], "restored": False}, request_id)
        emit("mcp_status", {"status": "disabled"}, request_id)
    elif kind == "prompt":
        prompt_count += 1
        turn_id = f"turn-{prompt_count}"
        emit("turn_started", {"name": request["payload"]["name"]}, request_id, turn=turn_id)
        if mode == "timeout":
            time.sleep(60)
            continue
        if mode == "oversized":
            emit("assistant_delta", {"text": "x" * 4096}, request_id, turn=turn_id)
        emit(
            "token_usage",
            {"input": 10, "output": 20, "reasoning": None, "cache_read": None, "cache_write": None, "total": 30},
            request_id,
            turn=turn_id,
        )
        response = "invalid" if mode == "repair" and prompt_count == 1 else VALID
        emit(
            "turn_finished",
            {
                "name": request["payload"]["name"],
                "final_response": response,
                "finish_reason": "stop",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "context_compacted": False,
                "sdk_retry_count": None,
            },
            request_id,
            turn=turn_id,
        )
    elif kind == "abort":
        emit("error", {"code": "ABORTED", "message": "aborted"}, request_id)
    elif kind == "dispose":
        emit("disposed", {}, request_id)
        break

os._exit(0)
