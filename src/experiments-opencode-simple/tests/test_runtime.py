from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from opencode_simple_agent.protocol import ProtocolError
from opencode_simple_agent.runtime import SidecarError, SidecarRuntime

FAKE = Path(__file__).with_name("fake_sidecar.py")


def runtime(mode: str, *, event_bytes: int = 16_384, total_bytes: int = 65_536) -> SidecarRuntime:
    return SidecarRuntime(
        [sys.executable, str(FAKE), mode],
        message_bytes=16_384,
        stderr_bytes=4096,
        event_output_bytes=event_bytes,
        total_event_bytes=total_bytes,
        dispose_grace_seconds=1,
        host_cwd=FAKE.parent,
    )


def initialize(value: SidecarRuntime) -> None:
    value.start()
    ready = value.initialize({}, timeout=2)
    assert ready.type == "ready"


def test_successful_jsonl_lifecycle_and_token_normalization() -> None:
    value = runtime("success")
    try:
        initialize(value)
        session, mcp = value.start_session({}, timeout=2)
        assert session.session_id == "fake-session"
        assert mcp.payload["status"] == "disabled"
        turn = value.prompt("initial", "prompt", timeout=2)
        assert turn.turn_id == "turn-1"
        assert turn.usage["total"] == 30
        assert turn.model == "gpt-5.6-sol"
    finally:
        value.dispose()
    assert value.process is not None and value.process.poll() == 0


@pytest.mark.parametrize("mode,error", [("malformed", ProtocolError), ("crash", SidecarError)])
def test_malformed_stdout_and_early_crash_fail_closed(mode: str, error: type[BaseException]) -> None:
    value = runtime(mode)
    value.start()
    try:
        with pytest.raises(error):
            value.initialize({}, timeout=2)
    finally:
        value.dispose()
    assert value.process is not None and value.process.poll() is not None


def test_event_and_cumulative_limits_are_enforced() -> None:
    value = runtime("oversized", event_bytes=512)
    try:
        initialize(value)
        value.start_session({}, timeout=2)
        with pytest.raises(ProtocolError, match="event exceeds"):
            value.prompt("initial", "prompt", timeout=2)
    finally:
        value.dispose()


def test_timeout_dispose_reaps_process_group() -> None:
    value = runtime("timeout")
    try:
        initialize(value)
        value.start_session({}, timeout=2)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            value.prompt("initial", "prompt", timeout=0.1)
        assert time.monotonic() - started < 2
    finally:
        value.dispose()
    assert value.process is not None and value.process.poll() is not None
