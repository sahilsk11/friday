"""OpencodeProvider persistence tests using pytest-httpx canned responses.

A live integration test is provided via ``scripts/probe_session_manager.py``
(run against a real opencode 1.14 server); the unit tests here pin the wire
shapes captured during the probe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from friday.core.opencode_provider import OpencodeProvider
from friday.core.provider import Message, SessionInfo

BASE_URL = "http://opencode.test"


def _row(
    *,
    sid: str = "ses_1",
    title: str = "smoke",
    directory: str = "/x",
    created: int = 1_700_000_000_000,
    updated: int = 1_700_000_001_000,
) -> dict[str, Any]:
    return {
        "id": sid,
        "title": title,
        "directory": directory,
        "time": {"created": created, "updated": updated},
    }


@pytest.fixture
def provider() -> OpencodeProvider:
    """Non-started OpencodeProvider for HTTP-only tests."""
    return OpencodeProvider(BASE_URL)


async def test_list_sessions_parses_rows(
    httpx_mock: HTTPXMock, provider: OpencodeProvider
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/experimental/session",
        json=[
            _row(sid="ses_a", title="alpha", directory="/x", created=1_700_000_000_000),
            _row(sid="ses_b", title="beta", directory="/y", created=1_700_000_500_000),
        ],
    )

    sessions = await provider.list_sessions()

    assert sessions == [
        SessionInfo(
            id="ses_a",
            title="alpha",
            directory="/x",
            created_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
            updated_at=datetime(2023, 11, 14, 22, 13, 21, tzinfo=UTC),
        ),
        SessionInfo(
            id="ses_b",
            title="beta",
            directory="/y",
            created_at=datetime(2023, 11, 14, 22, 21, 40, tzinfo=UTC),
            updated_at=datetime(2023, 11, 14, 22, 13, 21, tzinfo=UTC),
        ),
    ]


async def test_list_sessions_filters_by_directory(
    httpx_mock: HTTPXMock, provider: OpencodeProvider
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/experimental/session",
        json=[_row(sid="ses_a", directory="/keep"), _row(sid="ses_b", directory="/skip")],
    )

    sessions = await provider.list_sessions(directory="/keep")

    assert [s.id for s in sessions] == ["ses_a"]


async def test_get_returns_one_session(
    httpx_mock: HTTPXMock, provider: OpencodeProvider
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/session/ses_a",
        json=_row(sid="ses_a", title="probe"),
    )

    info = await provider.get_session("ses_a")

    assert info.id == "ses_a"
    assert info.title == "probe"


async def test_get_transcript_concatenates_text_parts(
    httpx_mock: HTTPXMock, provider: OpencodeProvider
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/session/ses_a/message",
        json=[
            {
                "info": {"role": "user", "time": {"created": 1}, "id": "msg_1"},
                "parts": [{"type": "text", "text": "hello"}],
            },
            {
                "info": {
                    "role": "assistant",
                    "time": {"created": 1, "completed": 2_000},
                    "id": "msg_2",
                },
                "parts": [
                    {"type": "step-start"},
                    {"type": "text", "text": "Hi "},
                    {"type": "text", "text": "there"},
                    {"type": "step-finish"},
                ],
            },
        ],
    )

    transcript = await provider.get_transcript("ses_a")

    assert len(transcript) == 2
    assert transcript[0] == Message(
        role="user",
        text="hello",
        completed_at=None,
        parts=[{"type": "text", "text": "hello"}],
    )
    assert transcript[1].role == "assistant"
    assert transcript[1].text == "Hi there"
    assert transcript[1].completed_at == datetime.fromtimestamp(2.0, tz=UTC)
    assert len(transcript[1].parts) == 4


async def test_get_transcript_accepts_legacy_time_end(
    httpx_mock: HTTPXMock, provider: OpencodeProvider
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/session/ses_a/message",
        json=[
            {
                "info": {"role": "assistant", "time": {"created": 1, "end": 5_000}},
                "parts": [{"type": "text", "text": "ok"}],
            },
        ],
    )

    transcript = await provider.get_transcript("ses_a")

    assert transcript[0].completed_at == datetime.fromtimestamp(5.0, tz=UTC)


async def test_create_returns_session_with_id(
    httpx_mock: HTTPXMock, provider: OpencodeProvider
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE_URL}/session",
        json={"id": "ses_new", "title": "fresh"},
    )

    session = await provider.create_session("fresh")

    assert session.id == "ses_new"


async def test_attach_returns_same_instance(provider: OpencodeProvider) -> None:
    a = provider.attach("ses_x")
    b = provider.attach("ses_x")
    assert a is b
