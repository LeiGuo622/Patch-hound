"""Strict versioned JSONL protocol shared with the TypeScript sidecar."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1
HOST_TYPES = {"initialize", "start_session", "prompt", "abort", "dispose"}
SIDECAR_TYPES = {
    "ready",
    "session_started",
    "mcp_status",
    "turn_started",
    "assistant_delta",
    "reasoning_delta",
    "tool_started",
    "tool_updated",
    "tool_finished",
    "token_usage",
    "permission_denied",
    "turn_finished",
    "error",
    "disposed",
}


class ProtocolError(RuntimeError):
    """Raised for malformed messages or illegal protocol state."""


@dataclass(frozen=True)
class ProtocolMessage:
    type: str
    id: str
    timestamp: str
    payload: dict[str, Any]
    request_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "type": self.type,
            "id": self.id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
        if self.request_id is not None:
            result["request_id"] = self.request_id
        if self.session_id is not None:
            result["session_id"] = self.session_id
        if self.turn_id is not None:
            result["turn_id"] = self.turn_id
        return result

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


def host_message(message_type: str, payload: dict[str, Any]) -> ProtocolMessage:
    if message_type not in HOST_TYPES:
        raise ProtocolError(f"unknown host message type: {message_type}")
    return ProtocolMessage(message_type, uuid4().hex, _now(), payload)


def parse_message(raw: bytes | str, *, max_bytes: int) -> ProtocolMessage:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > max_bytes:
        raise ProtocolError(f"protocol message exceeds {max_bytes} bytes")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"sidecar stdout contained invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("protocol message must be a JSON object")
    allowed = {"schema_version", "type", "id", "timestamp", "payload", "request_id", "session_id", "turn_id"}
    unknown = set(value) - allowed
    if unknown:
        raise ProtocolError(f"protocol message contains unknown fields: {sorted(unknown)}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError(f"unsupported schema_version: {value.get('schema_version')}")
    message_type = value.get("type")
    if not isinstance(message_type, str) or message_type not in SIDECAR_TYPES:
        raise ProtocolError(f"unknown sidecar message type: {message_type}")
    message_id = value.get("id")
    timestamp = value.get("timestamp")
    payload = value.get("payload")
    if not isinstance(message_id, str) or not message_id:
        raise ProtocolError("protocol id must be a non-empty string")
    if not isinstance(timestamp, str) or not timestamp:
        raise ProtocolError("protocol timestamp must be a non-empty string")
    if not isinstance(payload, dict):
        raise ProtocolError("protocol payload must be an object")
    for key in ("request_id", "session_id", "turn_id"):
        if key in value and (not isinstance(value[key], str) or not value[key]):
            raise ProtocolError(f"{key} must be a non-empty string when present")
    return ProtocolMessage(
        type=message_type,
        id=message_id,
        timestamp=timestamp,
        payload=payload,
        request_id=value.get("request_id"),
        session_id=value.get("session_id"),
        turn_id=value.get("turn_id"),
    )


class Phase(Enum):
    NEW = "new"
    READY = "ready"
    SESSION = "session"
    TURN = "turn"
    DISPOSED = "disposed"


class ProtocolState:
    """Validate ordering and stable session/turn identifiers."""

    def __init__(self) -> None:
        self.phase = Phase.NEW
        self.seen_ids: set[str] = set()
        self.session_id: str | None = None
        self.turn_id: str | None = None

    def accept(self, message: ProtocolMessage) -> None:
        if message.id in self.seen_ids:
            raise ProtocolError(f"duplicate protocol id: {message.id}")
        self.seen_ids.add(message.id)
        if message.type == "error":
            if self.session_id is not None and message.session_id is not None and message.session_id != self.session_id:
                raise ProtocolError(f"unexpected session_id: {message.session_id}")
            if self.turn_id is not None and message.turn_id is not None and message.turn_id != self.turn_id:
                raise ProtocolError(f"unexpected turn_id: {message.turn_id}")
            return
        if self.phase is Phase.NEW:
            if message.type != "ready":
                raise ProtocolError(f"expected ready, got {message.type}")
            self.phase = Phase.READY
            return
        if self.phase is Phase.READY:
            if message.type == "disposed":
                self.phase = Phase.DISPOSED
                return
            if message.type != "session_started" or not message.session_id:
                raise ProtocolError(f"expected session_started, got {message.type}")
            self.session_id = message.session_id
            self.phase = Phase.SESSION
            return
        if message.session_id is not None and message.session_id != self.session_id:
            raise ProtocolError(f"unexpected session_id: {message.session_id}")
        if self.phase is Phase.SESSION:
            if message.type in {"mcp_status", "permission_denied"}:
                return
            if message.type == "disposed":
                self.phase = Phase.DISPOSED
                return
            if message.type != "turn_started" or not message.turn_id:
                raise ProtocolError(f"expected turn_started, got {message.type}")
            self.turn_id = message.turn_id
            self.phase = Phase.TURN
            return
        if self.phase is Phase.TURN:
            if message.turn_id is not None and message.turn_id != self.turn_id:
                raise ProtocolError(f"unexpected turn_id: {message.turn_id}")
            if message.type == "disposed":
                self.turn_id = None
                self.phase = Phase.DISPOSED
                return
            if message.type == "turn_finished":
                self.turn_id = None
                self.phase = Phase.SESSION
                return
            if message.type not in {
                "assistant_delta",
                "reasoning_delta",
                "tool_started",
                "tool_updated",
                "tool_finished",
                "token_usage",
                "permission_denied",
            }:
                raise ProtocolError(f"illegal message during turn: {message.type}")
            return
        raise ProtocolError(f"message received after disposal: {message.type}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
