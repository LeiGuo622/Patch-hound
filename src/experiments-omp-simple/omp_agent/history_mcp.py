"""Direct MCP verification and a host-side stdio broker for the isolated server."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import socket
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_HISTORY_SERVER, DEFAULT_LINUX_REPO, DEFAULT_METADATA_DIR, PROJECT_ROOT

SERVER_NAME = "patchhound_history"
TOOL_NAME = "query_pre_patch_history"
# OMP SDK custom tools are registered directly and therefore retain the MCP
# tool name; OpenCode's client-side namespace prefix is not used here.
EXPOSED_TOOL_NAME = TOOL_NAME
PROTOCOL_VERSION = "2025-06-18"


class HistoryMcpError(RuntimeError):
    """Raised when history MCP discovery, schema, or transport fails."""


@dataclass(frozen=True)
class HistoryMcpDefinition:
    server_name: str
    tool_name: str
    schema: dict[str, Any]
    schema_sha256: str


@dataclass(frozen=True)
class HistoryMcpProbeResult:
    definition: HistoryMcpDefinition
    call_succeeded: bool
    response_bytes: int
    response_is_error: bool


def direct_probe(
    *,
    case_id: str,
    linux_repo: Path = DEFAULT_LINUX_REPO,
    metadata_dir: Path = DEFAULT_METADATA_DIR,
    query_log: Path | None = None,
    call_arguments: dict[str, Any] | None = None,
) -> HistoryMcpProbeResult:
    process = _start_server(case_id, linux_repo, metadata_dir, query_log)
    try:
        initialize = _rpc(
            process,
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "patchhound-omp-direct-probe", "version": "0.1.0"},
            },
        )
        server_info = _result_object(initialize).get("serverInfo")
        if not isinstance(server_info, dict) or server_info.get("name") != SERVER_NAME:
            raise HistoryMcpError(f"unexpected MCP server identity: {server_info}")
        _notify(process, "notifications/initialized", {})
        listed = _result_object(_rpc(process, 2, "tools/list", {}))
        tools = listed.get("tools")
        if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
            raise HistoryMcpError("history MCP must expose exactly one tool")
        tool = tools[0]
        if tool.get("name") != TOOL_NAME:
            raise HistoryMcpError(f"unexpected history MCP tool: {tool.get('name')}")
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            raise HistoryMcpError("history MCP tool has no input schema")
        definition = HistoryMcpDefinition(SERVER_NAME, TOOL_NAME, schema, schema_hash(schema))
        arguments = call_arguments or {
            "kind": "file_history",
            "file_path": "README",
            "max_events": 1,
            "analysis_stage": "patch security review",
            "reason": "P4 MCP transport probe",
        }
        called = _result_object(_rpc(process, 3, "tools/call", {"name": TOOL_NAME, "arguments": arguments}))
        raw = json.dumps(called, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return HistoryMcpProbeResult(
            definition=definition,
            call_succeeded=called.get("isError") is False,
            response_bytes=len(raw),
            response_is_error=called.get("isError") is True,
        )
    finally:
        _stop_process(process)


class HistoryMcpBroker:
    """Expose the existing stdio MCP server over a private Unix socket.

    The real server remains outside the Agent mount namespace so raw Git and
    metadata paths cannot be reached through bash. The in-sandbox proxy is a
    byte-for-byte transport only.
    """

    def __init__(
        self,
        *,
        socket_path: Path,
        case_id: str,
        linux_repo: Path,
        metadata_dir: Path,
        query_log: Path,
        stderr_bytes: int = 64 * 1024,
    ) -> None:
        self.socket_path = socket_path
        self.case_id = case_id
        self.linux_repo = linux_repo.expanduser().resolve()
        self.metadata_dir = metadata_dir.expanduser().resolve()
        self.query_log = query_log
        self.stderr_bytes = stderr_bytes
        self.process: subprocess.Popen[bytes] | None = None
        self.listener: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.error: BaseException | None = None
        self.method_counts: Counter[str] = Counter()
        self.stderr_chunks: deque[bytes] = deque()

    @property
    def stderr_summary(self) -> str:
        return b"".join(self.stderr_chunks)[-self.stderr_bytes :].decode("utf-8", errors="replace")

    def start(self) -> None:
        if self.thread is not None:
            raise HistoryMcpError("history MCP broker is already started")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.process = _start_server(self.case_id, self.linux_repo, self.metadata_dir, self.query_log)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.socket_path))
        except OSError as exc:
            self.listener.close()
            self.listener = None
            _stop_process(self.process)
            self.process = None
            self.socket_path.unlink(missing_ok=True)
            raise HistoryMcpError(f"cannot create private history MCP socket: {exc}") from exc
        os.chmod(self.socket_path, 0o600)
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, name="patchhound-history-mcp-broker", daemon=True)
        self.thread.start()
        self.ready_event.set()

    def check(self) -> None:
        if self.error is not None:
            raise HistoryMcpError(f"history MCP broker failed: {self.error}") from self.error
        if self.process is None or self.process.poll() is not None:
            code = self.process.returncode if self.process is not None else None
            raise HistoryMcpError(f"history MCP server exited unexpectedly: {code}; {self.stderr_summary}")

    def close(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            self.listener.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.process is not None:
            _stop_process(self.process)
        self.socket_path.unlink(missing_ok=True)

    def __enter__(self) -> HistoryMcpBroker:
        self.start()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _serve(self) -> None:
        try:
            connection: socket.socket | None = None
            while not self.stop_event.is_set() and connection is None:
                try:
                    assert self.listener is not None
                    connection, _ = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        return
                    raise
            if connection is None:
                return
            with connection:
                self._forward(connection)
        except BaseException as exc:  # noqa: BLE001
            if not self.stop_event.is_set():
                self.error = exc

    def _forward(self, connection: socket.socket) -> None:
        assert self.process is not None
        assert self.process.stdin is not None and self.process.stdout is not None and self.process.stderr is not None
        selector = selectors.DefaultSelector()
        connection.setblocking(True)
        os.set_blocking(self.process.stdout.fileno(), False)
        os.set_blocking(self.process.stderr.fileno(), False)
        selector.register(connection, selectors.EVENT_READ, "client")
        selector.register(self.process.stdout, selectors.EVENT_READ, "server")
        selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        client_buffer = b""
        while not self.stop_event.is_set():
            if self.process.poll() is not None:
                raise HistoryMcpError(f"history MCP server exited with {self.process.returncode}")
            for key, _ in selector.select(timeout=0.2):
                if key.data == "client":
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    client_buffer += chunk
                    while b"\n" in client_buffer:
                        line, client_buffer = client_buffer.split(b"\n", 1)
                        self._observe_request(line)
                        self.process.stdin.write(line + b"\n")
                        self.process.stdin.flush()
                elif key.data == "server":
                    try:
                        chunk = os.read(self.process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        return
                    connection.sendall(chunk)
                else:
                    try:
                        chunk = os.read(self.process.stderr.fileno(), 4096)
                    except BlockingIOError:
                        continue
                    if chunk:
                        self.stderr_chunks.append(chunk)
                        while sum(map(len, self.stderr_chunks)) > self.stderr_bytes:
                            self.stderr_chunks.popleft()

    def _observe_request(self, line: bytes) -> None:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            self.method_counts["invalid_json"] += 1
            return
        if isinstance(value, dict) and isinstance(value.get("method"), str):
            self.method_counts[value["method"]] += 1


def schema_hash(schema: dict[str, Any]) -> str:
    raw = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _start_server(
    case_id: str,
    linux_repo: Path,
    metadata_dir: Path,
    query_log: Path | None,
) -> subprocess.Popen[bytes]:
    if not DEFAULT_HISTORY_SERVER.is_file():
        raise FileNotFoundError(DEFAULT_HISTORY_SERVER)
    if not linux_repo.expanduser().resolve().is_dir():
        raise FileNotFoundError(linux_repo)
    if not metadata_dir.expanduser().resolve().is_dir():
        raise FileNotFoundError(metadata_dir)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATCHHOUND_CASE_ID": case_id,
        "PATCHHOUND_PROJECT_ROOT": str(PROJECT_ROOT),
        "PATCHHOUND_METADATA_DIR": str(metadata_dir.expanduser().resolve()),
        "PATCHHOUND_LINUX_REPO": str(linux_repo.expanduser().resolve()),
    }
    if query_log is not None:
        env["PATCHHOUND_HISTORY_QUERY_LOG"] = str(query_log.expanduser().resolve())
    return subprocess.Popen(
        [sys.executable, "-B", str(DEFAULT_HISTORY_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )


def _rpc(
    process: subprocess.Popen[bytes],
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
    process.stdin.flush()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    if not selector.select(max(0, deadline - time.monotonic())):
        raise TimeoutError(f"history MCP timed out waiting for {method}")
    line = process.stdout.readline(1024 * 1024)
    if not line:
        raise HistoryMcpError(f"history MCP closed stdout during {method}")
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("id") != request_id:
        raise HistoryMcpError(f"invalid MCP response for {method}: {value}")
    if "error" in value:
        raise HistoryMcpError(f"MCP {method} failed: {value['error']}")
    return value


def _notify(process: subprocess.Popen[bytes], method: str, params: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
    process.stdin.flush()


def _result_object(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("result")
    if not isinstance(value, dict):
        raise HistoryMcpError("MCP response result must be an object")
    return value


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
