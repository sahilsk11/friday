"""Parser tests using captured opencode SSE payloads."""

from __future__ import annotations

from friday.core.events import (
    MessagePartDelta,
    MessagePartUpdated,
    MessageUpdated,
    ServerConnected,
    SessionIdle,
    SessionStatus,
    parse_event,
)


def test_server_connected_unwrapped() -> None:
    raw = {"payload": {"type": "server.connected", "properties": {}}}
    event = parse_event(raw)
    assert isinstance(event, ServerConnected)


def test_session_status_busy() -> None:
    raw = {
        "directory": "/x",
        "project": "global",
        "payload": {
            "type": "session.status",
            "properties": {"sessionID": "ses_a", "status": {"type": "busy"}},
        },
    }
    event = parse_event(raw)
    assert isinstance(event, SessionStatus)
    assert event.session_id == "ses_a"
    assert event.status == "busy"


def test_session_idle() -> None:
    raw = {"payload": {"type": "session.idle", "properties": {"sessionID": "ses_a"}}}
    event = parse_event(raw)
    assert isinstance(event, SessionIdle)
    assert event.session_id == "ses_a"


def test_message_updated_user_role() -> None:
    raw = {
        "payload": {
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_a",
                "info": {
                    "id": "msg_1",
                    "role": "user",
                    "time": {"created": 1},
                },
            },
        },
    }
    event = parse_event(raw)
    assert isinstance(event, MessageUpdated)
    assert event.role == "user"
    assert event.time_end is None


def test_message_updated_assistant_completed_via_completed() -> None:
    raw = {
        "payload": {
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_a",
                "info": {
                    "id": "msg_2",
                    "role": "assistant",
                    "time": {"created": 1, "completed": 1234567890},
                },
            },
        },
    }
    event = parse_event(raw)
    assert isinstance(event, MessageUpdated)
    assert event.time_end == 1234567890


def test_message_updated_assistant_completed_via_end_legacy() -> None:
    raw = {
        "payload": {
            "type": "message.updated",
            "properties": {
                "sessionID": "ses_a",
                "info": {
                    "id": "msg_2",
                    "role": "assistant",
                    "time": {"created": 1, "end": 999},
                },
            },
        },
    }
    event = parse_event(raw)
    assert isinstance(event, MessageUpdated)
    assert event.time_end == 999


def test_message_part_updated_text_echo() -> None:
    raw = {
        "payload": {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_a",
                "part": {
                    "type": "text",
                    "text": "hello",
                    "messageID": "msg_1",
                    "id": "prt_1",
                },
                "time": 1,
            },
        },
    }
    event = parse_event(raw)
    assert isinstance(event, MessagePartUpdated)
    assert event.part_type == "text"
    assert event.text == "hello"
    assert event.tool_name is None


def test_message_part_delta_token() -> None:
    raw = {
        "payload": {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_a",
                "messageID": "msg_2",
                "partID": "prt_2",
                "field": "text",
                "delta": "Hi",
            },
        },
    }
    event = parse_event(raw)
    assert isinstance(event, MessagePartDelta)
    assert event.delta == "Hi"
    assert event.field == "text"


def test_sync_wrapper_is_dropped() -> None:
    raw = {
        "payload": {
            "type": "sync",
            "syncEvent": {"type": "message.updated.1", "data": {}},
        },
    }
    assert parse_event(raw) is None


def test_unknown_event_returns_none() -> None:
    raw = {"payload": {"type": "session.diff", "properties": {}}}
    assert parse_event(raw) is None
