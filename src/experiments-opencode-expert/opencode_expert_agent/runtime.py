"""Bounded lifecycle manager for the JSONL OpenCode sidecar."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .events import TurnExecution, normalize_message
from .protocol import ProtocolError, ProtocolMessage, ProtocolState, host_message, parse_message


class SidecarError(RuntimeError):
    """Structured sidecar/runtime failure."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class SidecarRuntime:
    def __init__(
        self,
        command: list[str],
        *,
        message_bytes: int,
        stderr_bytes: int,
        event_output_bytes: int | None = None,
        total_event_bytes: int | None = None,
        dispose_grace_seconds: int,
        host_cwd: Path,
    ) -> None:
        self.command = list(command)
        self.message_bytes = message_bytes
        self.stderr_bytes = stderr_bytes
        self.event_output_bytes = event_output_bytes or message_bytes
        self.total_event_bytes = total_event_bytes or message_bytes * 8
        self.event_bytes = 0
        self.dispose_grace_seconds = dispose_grace_seconds
        self.host_cwd = host_cwd
        self.process: subprocess.Popen[bytes] | None = None
        self.state = ProtocolState()
        self.stdout_queue: queue.Queue[bytes | BaseException | None] = queue.Queue()
        self.stderr_chunks: deque[bytes] = deque()
        self.stderr_size = 0
        self._threads: list[threading.Thread] = []
        self._closed = False

    @property
    def stderr_summary(self) -> str:
        raw = b"".join(self.stderr_chunks)[-self.stderr_bytes :]
        return raw.decode("utf-8", errors="replace")

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def start(self) -> None:
        if self.process is not None:
            raise SidecarError("STATE_ERROR", "sidecar is already started")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.host_cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise SidecarError("SIDECAR_START_FAILED", str(exc)) from exc
        self._threads = [
            threading.Thread(target=self._read_stdout, name="opencode-sidecar-stdout", daemon=True),
            threading.Thread(target=self._read_stderr, name="opencode-sidecar-stderr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def initialize(self, payload: dict[str, Any], *, timeout: float) -> ProtocolMessage:
        request = self._send("initialize", payload)
        return self._expect("ready", request.id, timeout)

    def start_session(self, payload: dict[str, Any], *, timeout: float) -> tuple[ProtocolMessage, ProtocolMessage]:
        request = self._send("start_session", payload)
        started = self._expect("session_started", request.id, timeout)
        mcp = self._expect("mcp_status", request.id, timeout)
        return started, mcp

    def prompt(self, name: str, text: str, *, timeout: float) -> TurnExecution:
        request = self._send("prompt", {"name": name, "text": text, "timeout_seconds": timeout})
        started = self._expect("turn_started", request.id, timeout)
        deadline = time.monotonic() + timeout
        events = [normalize_message(started)]
        usage: dict[str, Any] = {}
        while True:
            message = self._receive(_remaining(deadline))
            if message.request_id != request.id:
                raise ProtocolError(f"unexpected request_id during turn: {message.request_id}")
            if message.type == "error":
                self._raise_sidecar_error(message)
            events.append(normalize_message(message))
            if message.type == "token_usage":
                usage = dict(message.payload)
            if message.type != "turn_finished":
                continue
            payload = message.payload
            final_response = payload.get("final_response")
            if not isinstance(final_response, str):
                raise ProtocolError("turn_finished.final_response must be a string")
            turn_id = message.turn_id or started.turn_id
            if turn_id is None:
                raise ProtocolError("turn_finished is missing turn_id")
            return TurnExecution(
                name=name,
                turn_id=turn_id,
                final_response=final_response,
                finish_reason=_optional_string(payload.get("finish_reason")),
                provider=_optional_string(payload.get("provider")),
                model=_optional_string(payload.get("model")),
                reasoning_effort=_optional_string(payload.get("reasoning_effort")),
                usage=usage,
                events=tuple(events),
                context_compacted=bool(payload.get("context_compacted", False)),
                sdk_retry_count=payload.get("sdk_retry_count") if isinstance(payload.get("sdk_retry_count"), int) else None,
            )

    def abort(self, *, timeout: float = 5) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        request = self._send("abort", {})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._receive(_remaining(deadline))
            if message.type == "error" and message.request_id == request.id:
                return
            if message.type == "turn_finished":
                return

    def dispose(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        graceful = False
        try:
            if process.poll() is None:
                request = self._send("dispose", {})
                self._expect("disposed", request.id, self.dispose_grace_seconds)
                graceful = True
        except (ProtocolError, SidecarError, TimeoutError, BrokenPipeError):
            pass
        if graceful and process.poll() is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=self.dispose_grace_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process_group()
        else:
            self._terminate_process_group()
        for thread in self._threads:
            thread.join(timeout=self.dispose_grace_seconds)

    def __enter__(self) -> SidecarRuntime:
        self.start()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.dispose()

    def _send(self, message_type: str, payload: dict[str, Any]) -> ProtocolMessage:
        if self.process is None or self.process.stdin is None:
            raise SidecarError("STATE_ERROR", "sidecar is not started")
        if self.process.poll() is not None:
            raise SidecarError(
                "SIDECAR_EXITED",
                f"sidecar exited with code {self.process.returncode}",
                details={"stderr": self.stderr_summary},
            )
        message = host_message(message_type, payload)
        encoded = (message.to_json() + "\n").encode("utf-8")
        if len(encoded) > self.message_bytes:
            raise ProtocolError(f"outbound protocol message exceeds {self.message_bytes} bytes")
        self.process.stdin.write(encoded)
        self.process.stdin.flush()
        return message

    def _expect(self, expected_type: str, request_id: str, timeout: float) -> ProtocolMessage:
        message = self._receive(timeout)
        if message.request_id != request_id:
            raise ProtocolError(f"expected request_id {request_id}, got {message.request_id}")
        if message.type == "error":
            self._raise_sidecar_error(message)
        if message.type != expected_type:
            raise ProtocolError(f"expected {expected_type}, got {message.type}")
        return message

    def _receive(self, timeout: float) -> ProtocolMessage:
        try:
            item = self.stdout_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for sidecar protocol message") from exc
        if isinstance(item, BaseException):
            raise item
        if item is None:
            code = self.process.poll() if self.process is not None else None
            raise SidecarError(
                "SIDECAR_EOF",
                f"sidecar stdout closed before protocol completion (exit={code})",
                details={"stderr": self.stderr_summary},
            )
        message = parse_message(item.rstrip(b"\r\n"), max_bytes=self.message_bytes)
        if message.type in _EVENT_TYPES:
            size = len(item)
            if size > self.event_output_bytes:
                raise ProtocolError(f"sidecar event exceeds {self.event_output_bytes} bytes")
            self.event_bytes += size
            if self.event_bytes > self.total_event_bytes:
                raise ProtocolError(f"sidecar events exceed cumulative {self.total_event_bytes} bytes")
        self.state.accept(message)
        return message

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(self.message_bytes + 2)
                if not line:
                    self.stdout_queue.put(None)
                    return
                if len(line) > self.message_bytes + 1 or not line.endswith(b"\n"):
                    self.stdout_queue.put(ProtocolError("sidecar stdout message was oversized or unterminated"))
                    return
                self.stdout_queue.put(line)
        except BaseException as exc:  # noqa: BLE001
            self.stdout_queue.put(exc)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while chunk := os.read(self.process.stderr.fileno(), 4096):
            self.stderr_chunks.append(chunk)
            self.stderr_size += len(chunk)
            while self.stderr_size > self.stderr_bytes and self.stderr_chunks:
                removed = self.stderr_chunks.popleft()
                self.stderr_size -= len(removed)

    def _terminate_process_group(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.dispose_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=self.dispose_grace_seconds)

    @staticmethod
    def _raise_sidecar_error(message: ProtocolMessage) -> None:
        code = message.payload.get("code")
        detail = message.payload.get("message")
        raise SidecarError(
            code if isinstance(code, str) else "SIDECAR_ERROR",
            detail if isinstance(detail, str) else "sidecar reported an error",
            details=message.payload,
        )


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("the OpenCode run exceeded its total deadline")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


_EVENT_TYPES = {
    "turn_started",
    "assistant_delta",
    "reasoning_delta",
    "tool_started",
    "tool_updated",
    "tool_finished",
    "token_usage",
    "permission_denied",
    "turn_finished",
}
