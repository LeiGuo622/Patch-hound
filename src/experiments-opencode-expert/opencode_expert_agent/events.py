"""Stable event and turn result model for OpenCode sidecar messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import ProtocolMessage


@dataclass(frozen=True)
class NormalizedEvent:
    type: str
    timestamp: str
    turn_id: str | None
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "timestamp": self.timestamp,
            "turn_id": self.turn_id,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class TurnExecution:
    name: str
    turn_id: str
    final_response: str
    finish_reason: str | None
    provider: str | None
    model: str | None
    reasoning_effort: str | None
    usage: dict[str, Any]
    events: tuple[NormalizedEvent, ...]
    context_compacted: bool
    sdk_retry_count: int | None


def normalize_message(message: ProtocolMessage) -> NormalizedEvent:
    payload = dict(message.payload)
    if message.type == "reasoning_delta":
        payload.pop("text", None)
    return NormalizedEvent(message.type, message.timestamp, message.turn_id, payload)

