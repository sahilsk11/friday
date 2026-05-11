"""Compatibility shim for OpenCode provider event parsing."""

from __future__ import annotations

from friday.infra.providers.events import (
    MessagePartDelta,
    MessagePartUpdated,
    MessageUpdated,
    OpencodeEvent,
    ServerConnected,
    SessionError,
    SessionIdle,
    SessionStatus,
    parse_event,
)

__all__ = [
    "MessagePartDelta",
    "MessagePartUpdated",
    "MessageUpdated",
    "OpencodeEvent",
    "ServerConnected",
    "SessionError",
    "SessionIdle",
    "SessionStatus",
    "parse_event",
]
