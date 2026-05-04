"""Typed events emitted by the opencode SSE stream.

Wire envelope (see opencode v1.14.30, ``GET /global/event``)::

    {"directory": "...", "project": "global",
     "payload": {"type": "<event_type>", "properties": {...}}}

A separate ``sync`` event type wraps every payload again with a sequence number;
those are discarded here because the unwrapped event arrives on the same stream.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ServerConnected:
    """Emitted once when the SSE stream is established."""

    type: Literal["server.connected"] = "server.connected"


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """Session moved between busy / idle / etc.

    ``status`` is the literal ``status.type`` value from the wire (``"busy"``,
    ``"idle"``, ...). Mapped to ``AgentState`` at the consumer.
    """

    session_id: str
    status: str
    type: Literal["session.status"] = "session.status"


@dataclass(frozen=True, slots=True)
class SessionIdle:
    """Explicit "this session has nothing more to do" signal."""

    session_id: str
    type: Literal["session.idle"] = "session.idle"


@dataclass(frozen=True, slots=True)
class MessageUpdated:
    """A message's metadata changed — start, update, or completion.

    ``role`` is ``"user"`` or ``"assistant"``. ``time_end`` is set once the
    message is complete; that's how we detect "assistant turn done".

    ``model_id`` / ``provider_id`` come from ``info.modelID`` / ``info.providerID``
    on assistant messages. They're authoritative ground truth for "what model
    actually ran this turn" — used to drive the model chip in the UI.
    """

    session_id: str
    message_id: str
    role: str
    time_end: int | None
    model_id: str | None = None
    provider_id: str | None = None
    type: Literal["message.updated"] = "message.updated"


@dataclass(frozen=True, slots=True)
class MessagePartUpdated:
    """A message part (text or tool) was added or updated.

    For tool parts, ``tool_name`` and ``tool_status`` are populated. For text
    parts the streaming content arrives via :class:`MessagePartDelta`; this
    event is only useful for the user-message echo and for tool lifecycle.
    """

    session_id: str
    message_id: str
    part_id: str
    part_type: str
    text: str | None
    tool_name: str | None
    tool_status: str | None
    tool_input: dict[str, Any] = field(default_factory=dict)
    type: Literal["message.part.updated"] = "message.part.updated"


@dataclass(frozen=True, slots=True)
class MessagePartDelta:
    """Streaming token for a message part. Only ``field == "text"`` matters."""

    session_id: str
    message_id: str
    part_id: str
    field: str
    delta: str
    type: Literal["message.part.delta"] = "message.part.delta"


OpencodeEvent = (
    ServerConnected
    | SessionStatus
    | SessionIdle
    | MessageUpdated
    | MessagePartUpdated
    | MessagePartDelta
)


_Parser = Callable[[dict[str, Any]], OpencodeEvent]


def parse_event(raw: dict[str, Any]) -> OpencodeEvent | None:
    """Parse one SSE ``data:`` payload into a typed event.

    Returns ``None`` for ``sync`` wrappers and unknown event types so the
    caller can silently skip them.
    """
    payload = raw.get("payload", raw)
    event_type = payload.get("type")
    if event_type == "server.connected":
        return ServerConnected()
    parser = _PARSERS.get(event_type)
    if parser is None:
        return None
    return parser(payload.get("properties") or {})


def _parse_session_status(props: dict[str, Any]) -> OpencodeEvent:
    status = (props.get("status") or {}).get("type", "")
    return SessionStatus(session_id=props["sessionID"], status=status)


def _parse_session_idle(props: dict[str, Any]) -> OpencodeEvent:
    return SessionIdle(session_id=props["sessionID"])


def _parse_message_part_delta(props: dict[str, Any]) -> OpencodeEvent:
    return MessagePartDelta(
        session_id=props["sessionID"],
        message_id=props["messageID"],
        part_id=props["partID"],
        field=props.get("field", ""),
        delta=props.get("delta", ""),
    )


def _parse_message_updated(props: dict[str, Any]) -> OpencodeEvent:
    info = props.get("info") or {}
    time = info.get("time") or {}
    # opencode v1.14 uses ``time.completed``; older builds used ``time.end``.
    return MessageUpdated(
        session_id=props["sessionID"],
        message_id=info.get("id", ""),
        role=info.get("role", ""),
        time_end=time.get("completed") or time.get("end"),
        model_id=info.get("modelID"),
        provider_id=info.get("providerID"),
    )


def _parse_message_part_updated(props: dict[str, Any]) -> OpencodeEvent:
    part = props.get("part") or {}
    state = part.get("state") or {}
    return MessagePartUpdated(
        session_id=props["sessionID"],
        message_id=part.get("messageID", ""),
        part_id=part.get("id", ""),
        part_type=part.get("type", ""),
        text=part.get("text"),
        tool_name=part.get("tool"),
        tool_status=state.get("status"),
        tool_input=state.get("input") or {},
    )


_PARSERS: dict[str, _Parser] = {
    "session.status": _parse_session_status,
    "session.idle": _parse_session_idle,
    "message.updated": _parse_message_updated,
    "message.part.updated": _parse_message_part_updated,
    "message.part.delta": _parse_message_part_delta,
}
