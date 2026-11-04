from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from opencode_simple_agent.config import DEFAULT_METADATA_DIR, resolve_linux_repo
from opencode_simple_agent.history_mcp import EXPOSED_TOOL_NAME, HistoryMcpBroker, HistoryMcpError, direct_probe


def test_direct_history_probe_discovers_schema_and_calls_without_model() -> None:
    linux_repo = resolve_linux_repo()
    if not linux_repo.is_dir() or not DEFAULT_METADATA_DIR.is_dir():
        pytest.skip("PatchHound history repository or metadata is not available")
    result = direct_probe(
        case_id="case_0001",
        linux_repo=linux_repo,
        call_arguments={
            "kind": "function_history",
            "file_path": "drivers/net/ethernet/intel/ixgbe/ixgbe_xsk.c",
            "symbol": "ixgbe_xmit_zc",
            "max_events": 1,
            "analysis_stage": "test",
            "reason": "verify transport",
        },
    )
    assert result.call_succeeded
    assert result.definition.tool_name == "query_pre_patch_history"
    assert EXPOSED_TOOL_NAME == "patchhound_history_query_pre_patch_history"
    assert len(result.definition.schema_sha256) == 64


def test_private_socket_broker_forwards_json_rpc(tmp_path: Path) -> None:
    linux_repo = resolve_linux_repo()
    if not linux_repo.is_dir() or not DEFAULT_METADATA_DIR.is_dir():
        pytest.skip("PatchHound history repository or metadata is not available")
    broker = HistoryMcpBroker(
        socket_path=tmp_path / "runtime" / "history.sock",
        case_id="case_0001",
        linux_repo=linux_repo,
        metadata_dir=DEFAULT_METADATA_DIR,
        query_log=tmp_path / "history.md",
    )
    try:
        broker.start()
    except HistoryMcpError as exc:
        if "Operation not permitted" in str(exc):
            pytest.skip(f"execution environment blocks private Unix sockets: {exc}")
        raise
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(broker.socket_path))
            stream = client.makefile("rwb", buffering=0)
            stream.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
                    }
                ).encode()
                + b"\n"
            )
            response = json.loads(stream.readline())
            assert response["result"]["serverInfo"]["name"] == "patchhound_history"
            stream.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n')
            listed = json.loads(stream.readline())
            assert listed["result"]["tools"][0]["name"] == "query_pre_patch_history"
            assert "nextCursor" not in listed["result"]
        deadline = time.monotonic() + 2
        while broker.method_counts["tools/list"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        broker.check()
        assert broker.method_counts["initialize"] == 1
        assert broker.method_counts["tools/list"] == 1
    finally:
        broker.close()
