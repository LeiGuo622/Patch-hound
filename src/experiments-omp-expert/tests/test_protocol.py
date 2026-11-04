from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from omp_expert_agent.protocol import Phase, ProtocolError, ProtocolMessage, ProtocolState, parse_message


def raw(message_type: str, **extra: object) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "type": message_type,
            "id": f"id-{message_type}-{len(extra)}",
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": {},
            **extra,
        }
    )


def message(message_type: str, message_id: str, **extra: object) -> ProtocolMessage:
    return parse_message(
        json.dumps(
            {
                "schema_version": 1,
                "type": message_type,
                "id": message_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": {},
                **extra,
            }
        ),
        max_bytes=4096,
    )


@pytest.mark.parametrize(
    "value,match",
    [
        ("not-json", "invalid JSON"),
        (json.dumps({"schema_version": 2, "type": "ready", "id": "x", "timestamp": "now", "payload": {}}), "schema"),
        (raw("ready", surprise=True), "unknown fields"),
    ],
)
def test_rejects_malformed_messages(value: str, match: str) -> None:
    with pytest.raises(ProtocolError, match=match):
        parse_message(value, max_bytes=4096)


def test_rejects_oversized_message() -> None:
    with pytest.raises(ProtocolError, match="exceeds"):
        parse_message(raw("ready"), max_bytes=4)


def test_state_machine_correlation_duplicate_and_dispose_during_turn() -> None:
    state = ProtocolState()
    state.accept(message("ready", "1", request_id="init"))
    state.accept(message("session_started", "2", request_id="session", session_id="s1"))
    state.accept(message("mcp_status", "3", request_id="session", session_id="s1"))
    state.accept(message("turn_started", "4", request_id="turn", session_id="s1", turn_id="t1"))
    with pytest.raises(ProtocolError, match="session_id"):
        state.accept(message("assistant_delta", "5", request_id="turn", session_id="other", turn_id="t1"))

    state = ProtocolState()
    for item in (
        message("ready", "1", request_id="init"),
        message("session_started", "2", request_id="session", session_id="s1"),
        message("turn_started", "3", request_id="turn", session_id="s1", turn_id="t1"),
        message("disposed", "4", request_id="dispose", session_id="s1"),
    ):
        state.accept(item)
    assert state.phase is Phase.DISPOSED
    with pytest.raises(ProtocolError, match="duplicate"):
        state.accept(message("disposed", "4", request_id="dispose", session_id="s1"))


def test_error_messages_still_obey_session_and_turn_correlation() -> None:
    state = ProtocolState()
    state.accept(message("ready", "1", request_id="init"))
    state.accept(message("session_started", "2", request_id="session", session_id="s1"))
    state.accept(message("turn_started", "3", request_id="turn", session_id="s1", turn_id="t1"))
    with pytest.raises(ProtocolError, match="turn_id"):
        state.accept(
            ProtocolMessage(
                "error",
                "4",
                datetime.now(UTC).isoformat(),
                {"code": "FAILED"},
                request_id="turn",
                session_id="s1",
                turn_id="other",
            )
        )

