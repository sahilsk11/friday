"""Tests for OpencodeProcessor.

These exercise the adapter as a black box: feed it pipecat frames and
synthetic opencode events (via ``OpencodeSession.dispatch``), and assert on
the frames it pushes downstream.

We bypass the full pipecat lifecycle — no StartFrame, no Pipeline. Instead
we override ``push_frame`` on the processor instance to capture outputs.
That keeps tests fast and focused on the adapter's logic, not pipecat's
plumbing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pytest_httpx import HTTPXMock

from friday.core.events import MessagePartDelta, MessageUpdated, SessionStatus
from friday.core.opencode_session import OpencodeClient, OpencodeSession
from friday.voice.pipecat_adapter import DEFAULT_ACK_TEXT, OpencodeProcessor

OPENCODE_URL = "http://opencode.test"
SESSION_ID = "ses_a"


@pytest.fixture
async def session() -> AsyncIterator[OpencodeSession]:
    """A real OpencodeSession bound to a non-started client (no SSE loop)."""
    client = OpencodeClient(OPENCODE_URL)
    yield client.session(SESSION_ID)
    await client.aclose()


def _make_processor(session: OpencodeSession) -> tuple[OpencodeProcessor, list[Frame]]:
    """Build the processor and replace ``push_frame`` with a capture list.

    Returns the processor and a list that grows as frames are pushed.
    """
    pushed: list[Frame] = []
    proc = OpencodeProcessor(session)

    async def capture(frame: Frame, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    proc.push_frame = capture  # type: ignore[method-assign]
    return proc, pushed


def _transcription(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(
        text=text,
        user_id="u1",
        timestamp="2026-01-01T00:00:00Z",
        finalized=True,
    )


async def test_finalized_transcription_posts_turn_to_opencode(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hello world"), FrameDirection.DOWNSTREAM)

    sent = httpx_mock.get_requests()
    assert len(sent) == 1
    assert sent[0].url.path == f"/session/{SESSION_ID}/prompt_async"
    # Finalized transcription is consumed (it was the user turn) — not pushed downstream.
    assert pushed == []


async def test_non_finalized_transcription_passes_through(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    proc, pushed = _make_processor(session)
    interim = TranscriptionFrame(
        text="he", user_id="u1", timestamp="2026-01-01T00:00:00Z", finalized=False
    )

    await proc.process_frame(interim, FrameDirection.DOWNSTREAM)

    assert httpx_mock.get_requests() == []
    assert pushed == [interim]


async def test_immediate_ack_fires_on_busy_before_deltas(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hi"), FrameDirection.DOWNSTREAM)
    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="busy"))

    assert len(pushed) == 1
    ack = pushed[0]
    assert isinstance(ack, TTSSpeakFrame)
    assert ack.text == DEFAULT_ACK_TEXT


async def test_duplicate_busy_does_not_double_ack(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hi"), FrameDirection.DOWNSTREAM)
    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="busy"))
    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="busy"))

    assert sum(isinstance(f, TTSSpeakFrame) for f in pushed) == 1


async def test_ack_suppressed_when_deltas_arrive_first(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hi"), FrameDirection.DOWNSTREAM)
    # Race: the first delta arrives before opencode emits status:busy.
    await session.dispatch(
        MessagePartDelta(
            session_id=SESSION_ID, message_id="m1", part_id="p1", field="text", delta="Hi"
        )
    )
    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="busy"))

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


async def test_text_deltas_emit_bracketed_llm_frames(session: OpencodeSession) -> None:
    _, pushed = _make_processor(session)

    await session.dispatch(
        MessagePartDelta(
            session_id=SESSION_ID, message_id="m1", part_id="p1", field="text", delta="He"
        )
    )
    await session.dispatch(
        MessagePartDelta(
            session_id=SESSION_ID, message_id="m1", part_id="p1", field="text", delta="llo"
        )
    )
    await session.dispatch(
        MessageUpdated(session_id=SESSION_ID, message_id="m1", role="assistant", time_end=1_000)
    )

    types = [type(f).__name__ for f in pushed]
    assert types == [
        "LLMFullResponseStartFrame",
        "LLMTextFrame",
        "LLMTextFrame",
        "LLMFullResponseEndFrame",
    ]
    text_frames = [f for f in pushed if isinstance(f, LLMTextFrame)]
    assert [f.text for f in text_frames] == ["He", "llo"]
    assert isinstance(pushed[0], LLMFullResponseStartFrame)
    assert isinstance(pushed[-1], LLMFullResponseEndFrame)


async def test_final_without_deltas_emits_no_end_frame(session: OpencodeSession) -> None:
    _, pushed = _make_processor(session)

    await session.dispatch(
        MessageUpdated(session_id=SESSION_ID, message_id="m1", role="assistant", time_end=1_000)
    )

    assert pushed == []


async def test_two_back_to_back_turns_both_forwarded(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    proc, _ = _make_processor(session)

    await proc.process_frame(_transcription("alpha"), FrameDirection.DOWNSTREAM)
    await proc.process_frame(_transcription("beta"), FrameDirection.DOWNSTREAM)

    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    bodies = [httpx.Request.read(r) for r in requests]
    assert b"alpha" in bodies[0]
    assert b"beta" in bodies[1]
