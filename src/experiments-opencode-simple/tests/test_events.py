from __future__ import annotations

from datetime import UTC, datetime

from opencode_simple_agent.events import normalize_message
from opencode_simple_agent.protocol import ProtocolMessage


def test_reasoning_text_is_never_retained() -> None:
    message = ProtocolMessage(
        "reasoning_delta",
        "event-1",
        datetime.now(UTC).isoformat(),
        {"text": "hidden chain of thought", "chars": 23, "bytes": 23},
        request_id="request",
        session_id="session",
        turn_id="turn",
    )
    normalized = normalize_message(message)
    assert "text" not in normalized.payload
    assert normalized.payload["chars"] == 23
