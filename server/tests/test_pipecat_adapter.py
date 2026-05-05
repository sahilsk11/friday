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

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pytest_httpx import HTTPXMock

from friday.core.ack_generator import FALLBACK
from friday.core.events import (
    MessagePartDelta,
    MessagePartUpdated,
    MessageUpdated,
    SessionStatus,
)
from friday.core.opencode_session import ModelChoice, OpencodeClient, OpencodeSession
from friday.voice.pipecat_adapter import (
    RTVI_AGENT_STATE,
    RTVI_ASSISTANT_TEXT_DELTA,
    RTVI_ASSISTANT_TEXT_FINAL,
    RTVI_TOOL_STARTED,
    OpencodeProcessor,
)


@pytest.fixture(autouse=True)
def _no_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Clear OPENROUTER_API_KEY for every test in this module.

    The adapter spawns LLM calls (ack_generator, tool_narrator) when this is
    set; with the key, tests pick up the developer's real key from .env and
    either hit the network or — under httpx_mock — register unexpected POSTs
    that fail teardown. Tests that exercise the LLM-on path patch the call
    sites directly.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _rtvi_messages_of_type(pushed: list[Frame], type_name: str) -> list[dict[str, object]]:
    """Helper: extract RTVI server messages of a given ``type`` from pushed."""
    out: list[dict[str, object]] = []
    for f in pushed:
        if not isinstance(f, RTVIServerMessageFrame):
            continue
        data = f.data
        if isinstance(data, dict) and data.get("type") == type_name:
            out.append(data)
    return out


OPENCODE_URL = "http://opencode.test"
SESSION_ID = "ses_a"


@pytest.fixture
async def session() -> AsyncIterator[OpencodeSession]:
    """A real OpencodeSession bound to a non-started client (no SSE loop)."""
    client = OpencodeClient(OPENCODE_URL)
    yield client.session(SESSION_ID)
    await client.aclose()


_TEST_SYSTEM_PROMPT = "TEST_SYS_PROMPT"


def _make_processor(
    session: OpencodeSession, *, tts_enabled: bool = True
) -> tuple[OpencodeProcessor, list[Frame]]:
    """Build the processor and replace ``push_frame`` with a capture list.

    Returns the processor and a list that grows as frames are pushed.
    Tests default to ``tts_enabled=True`` so existing TTS-emission cases
    keep passing — production sets the flag from the client's speaker
    toggle (off by default on a fresh page load).
    """
    pushed: list[Frame] = []
    proc = OpencodeProcessor(session, system_prompt=_TEST_SYSTEM_PROMPT)
    proc.tts_enabled = tts_enabled

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
    # Finalized transcription is consumed (it was the user turn) — not pushed
    # downstream as a frame. The ack is a separate TTSSpeakFrame, asserted in
    # ``test_ack_fires_on_finalized_transcription``.
    assert not any(isinstance(f, TranscriptionFrame) for f in pushed)


async def test_next_turn_model_is_forwarded_then_cleared(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    """When the WS handler stamps ``next_turn_model`` from an end-turn message,
    the next finalized transcription forwards it to opencode and clears it."""
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
    proc.next_turn_model = ModelChoice(provider_id="opencode", model_id="gpt-5-nano")

    await proc.process_frame(_transcription("first"), FrameDirection.DOWNSTREAM)
    await proc.process_frame(_transcription("second"), FrameDirection.DOWNSTREAM)

    sent = httpx_mock.get_requests()
    assert json.loads(sent[0].content) == {
        "parts": [{"type": "text", "text": "first"}],
        "model": {"providerID": "opencode", "modelID": "gpt-5-nano"},
        "system": _TEST_SYSTEM_PROMPT,
    }
    # Second turn has no model — the field was consumed on the first turn,
    # opencode's per-session stickiness carries the choice forward. The system
    # prompt rides on every turn though (per-turn is the only injection path
    # opencode honors).
    assert json.loads(sent[1].content) == {
        "parts": [{"type": "text", "text": "second"}],
        "system": _TEST_SYSTEM_PROMPT,
    }
    assert proc.next_turn_model is None


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


async def test_ack_fires_on_finalized_transcription(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    """Without OPENROUTER_API_KEY the ack is the static fallback, but it
    still fires the moment the user's transcript finalizes — well before
    opencode would have emitted ``session.status:busy``."""
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hi"), FrameDirection.DOWNSTREAM)
    # Let the spawned ack task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    speak_frames = [f for f in pushed if isinstance(f, TTSSpeakFrame)]
    assert len(speak_frames) == 1
    assert speak_frames[0].text == FALLBACK


async def test_busy_state_does_not_emit_ack(session: OpencodeSession) -> None:
    """``session.status:busy`` is now a UI signal only — no TTSSpeakFrame from it.

    (Catches regressions where ack logic creeps back into ``_on_state``.)"""
    _, pushed = _make_processor(session)

    # No transcription yet, so no ack should fire from raw state changes.
    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="busy"))
    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="busy"))

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


async def test_ack_suppressed_when_deltas_arrive_first(
    httpx_mock: HTTPXMock, session: OpencodeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Race: assistant text starts streaming before the ack task gets a turn.

    Patch ``generate_ack`` to a slow path so the delta wins. The ack task
    must drop its phrase rather than talk over the assistant.
    """
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )

    async def slow_ack(_transcript: str) -> str:
        await asyncio.sleep(0.05)
        return "should not be spoken"

    monkeypatch.setattr("friday.voice.pipecat_adapter.generate_ack", slow_ack)
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hi"), FrameDirection.DOWNSTREAM)
    # Real text reaches the user before the slow ack returns.
    await session.dispatch(
        MessagePartDelta(
            session_id=SESSION_ID, message_id="m1", part_id="p1", field="text", delta="Hi"
        )
    )
    # Wait long enough for the slow ack to resolve.
    await asyncio.sleep(0.1)

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


async def test_rapid_second_turn_cancels_pending_ack(
    httpx_mock: HTTPXMock, session: OpencodeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two transcripts in quick succession: the first ack must be cancelled
    so its phrase can't speak over the second turn's ack."""
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

    call_count = 0

    async def slow_ack(transcript: str) -> str:
        nonlocal call_count
        call_count += 1
        # First call sleeps long enough that the second transcript arrives
        # mid-flight; second call returns instantly.
        if call_count == 1:
            await asyncio.sleep(0.2)
            return f"stale: {transcript}"
        return f"fresh: {transcript}"

    monkeypatch.setattr("friday.voice.pipecat_adapter.generate_ack", slow_ack)
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("alpha"), FrameDirection.DOWNSTREAM)
    # Fire the second turn before the first ack returns.
    await asyncio.sleep(0.01)
    await proc.process_frame(_transcription("beta"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.3)

    speak_frames = [f for f in pushed if isinstance(f, TTSSpeakFrame)]
    assert len(speak_frames) == 1
    assert speak_frames[0].text == "fresh: beta"


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

    # The LLM-frame subsequence must be exactly start → text → text → end,
    # in that order. RTVI server messages may interleave (delta + final +
    # agent-state) and are checked separately below.
    llm_types = [
        type(f).__name__
        for f in pushed
        if isinstance(f, (LLMFullResponseStartFrame, LLMTextFrame, LLMFullResponseEndFrame))
    ]
    assert llm_types == [
        "LLMFullResponseStartFrame",
        "LLMTextFrame",
        "LLMTextFrame",
        "LLMFullResponseEndFrame",
    ]
    text_frames = [f for f in pushed if isinstance(f, LLMTextFrame)]
    assert [f.text for f in text_frames] == ["He", "llo"]


async def test_final_without_deltas_emits_no_llm_end_frame(session: OpencodeSession) -> None:
    _, pushed = _make_processor(session)

    await session.dispatch(
        MessageUpdated(session_id=SESSION_ID, message_id="m1", role="assistant", time_end=1_000)
    )

    # No LLM frames — without a preceding delta there's nothing to bracket.
    assert not any(
        isinstance(f, (LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame))
        for f in pushed
    )
    # MessageUpdated still drives the session to IDLE; the agent-state RTVI
    # message lets the UI clear its "thinking" indicator.
    state_msgs = _rtvi_messages_of_type(pushed, RTVI_AGENT_STATE)
    assert state_msgs == [{"type": RTVI_AGENT_STATE, "state": "idle"}]


async def test_fenced_code_blocks_are_not_pushed_as_text(session: OpencodeSession) -> None:
    """Deltas inside ``` ... ``` should never become LLMTextFrames."""
    _, pushed = _make_processor(session)
    deltas = [
        "Here it is:\n",
        "```python\n",
        "def hi():\n",
        "    pass\n",
        "```\nDone.",
    ]
    for d in deltas:
        await session.dispatch(
            MessagePartDelta(
                session_id=SESSION_ID, message_id="m1", part_id="p1", field="text", delta=d
            )
        )
    await session.dispatch(
        MessageUpdated(session_id=SESSION_ID, message_id="m1", role="assistant", time_end=1)
    )

    text_frames = [f.text for f in pushed if isinstance(f, LLMTextFrame)]
    spoken = "".join(text_frames)
    assert "def hi" not in spoken
    assert "pass" not in spoken
    assert "Here it is:" in spoken
    assert "Done." in spoken


async def test_tool_start_emits_checkpoint_speak(session: OpencodeSession) -> None:
    proc, pushed = _make_processor(session)
    proc.narrate_tools = True

    await session.dispatch(
        MessagePartUpdated(
            session_id=SESSION_ID,
            message_id="m1",
            part_id="tp1",
            part_type="tool",
            text=None,
            tool_name="read",
            tool_status="running",
        )
    )
    await asyncio.sleep(0)  # let narration background task run

    speak_frames = [f for f in pushed if isinstance(f, TTSSpeakFrame)]
    assert len(speak_frames) == 1
    assert speak_frames[0].text == "looking at a file"


async def test_tool_narration_off_by_default(session: OpencodeSession) -> None:
    """With ``narrate_tools`` False (the default), tool starts surface to the
    activity feed via RTVI but never reach TTS."""
    _, pushed = _make_processor(session)

    await session.dispatch(
        MessagePartUpdated(
            session_id=SESSION_ID,
            message_id="m1",
            part_id="tp1",
            part_type="tool",
            text=None,
            tool_name="read",
            tool_status="running",
        )
    )
    await asyncio.sleep(0)

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)
    rtvi_tools = _rtvi_messages_of_type(pushed, RTVI_TOOL_STARTED)
    # No label is generated when narration is off — UI falls back to the name.
    assert rtvi_tools == [{"type": RTVI_TOOL_STARTED, "name": "read"}]


async def test_tool_status_updates_dont_double_announce(session: OpencodeSession) -> None:
    """Opencode emits MessagePartUpdated repeatedly per tool — only narrate once."""
    proc, pushed = _make_processor(session)
    proc.narrate_tools = True

    for status in ("pending", "running", "completed"):
        await session.dispatch(
            MessagePartUpdated(
                session_id=SESSION_ID,
                message_id="m1",
                part_id="tp1",
                part_type="tool",
                text=None,
                tool_name="read",
                tool_status=status,
            )
        )
    await asyncio.sleep(0)  # let narration background task run

    assert sum(isinstance(f, TTSSpeakFrame) for f in pushed) == 1


async def test_unknown_tool_emits_no_checkpoint(session: OpencodeSession) -> None:
    proc, pushed = _make_processor(session)
    proc.narrate_tools = True

    await session.dispatch(
        MessagePartUpdated(
            session_id=SESSION_ID,
            message_id="m1",
            part_id="tp1",
            part_type="tool",
            text=None,
            tool_name="some_made_up_tool",
            tool_status="running",
        )
    )
    await asyncio.sleep(0)  # let narration background task run

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


async def test_text_deltas_emit_rtvi_assistant_text_delta(session: OpencodeSession) -> None:
    """Every raw delta — fenced or not — surfaces to the UI as an RTVI message."""
    _, pushed = _make_processor(session)

    deltas = ["Here it is:\n", "```python\n", "x=1\n", "```\nDone."]
    for d in deltas:
        await session.dispatch(
            MessagePartDelta(
                session_id=SESSION_ID, message_id="m1", part_id="p1", field="text", delta=d
            )
        )

    rtvi_deltas = _rtvi_messages_of_type(pushed, RTVI_ASSISTANT_TEXT_DELTA)
    assert [m["text"] for m in rtvi_deltas] == deltas


async def test_message_finalized_emits_rtvi_assistant_text_final(session: OpencodeSession) -> None:
    _, pushed = _make_processor(session)

    await session.dispatch(
        MessagePartDelta(
            session_id=SESSION_ID, message_id="m1", part_id="p1", field="text", delta="hi there"
        )
    )
    await session.dispatch(
        MessageUpdated(session_id=SESSION_ID, message_id="m1", role="assistant", time_end=1)
    )

    finals = _rtvi_messages_of_type(pushed, RTVI_ASSISTANT_TEXT_FINAL)
    assert finals == [{"type": RTVI_ASSISTANT_TEXT_FINAL, "text": "hi there"}]


async def test_tool_start_emits_rtvi_for_unknown_tool_too(session: OpencodeSession) -> None:
    """Unknown tools still appear in the activity feed even when there's no
    spoken narration phrase."""
    proc, pushed = _make_processor(session)
    proc.narrate_tools = True

    await session.dispatch(
        MessagePartUpdated(
            session_id=SESSION_ID,
            message_id="m1",
            part_id="tp1",
            part_type="tool",
            text=None,
            tool_name="some_made_up_tool",
            tool_status="running",
        )
    )
    await asyncio.sleep(0)  # let narration background task run

    rtvi_tools = _rtvi_messages_of_type(pushed, RTVI_TOOL_STARTED)
    assert rtvi_tools == [{"type": RTVI_TOOL_STARTED, "name": "some_made_up_tool"}]
    # No spoken phrase — unknown tool, no OpenRouter key in test env.
    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


async def test_state_changes_emit_rtvi_agent_state(session: OpencodeSession) -> None:
    _, pushed = _make_processor(session)

    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="busy"))
    await session.dispatch(SessionStatus(session_id=SESSION_ID, status="idle"))

    states = _rtvi_messages_of_type(pushed, RTVI_AGENT_STATE)
    assert [s["state"] for s in states] == ["thinking", "idle"]


async def test_interruption_aborts_opencode_and_passes_frame_through(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    """The user tapped Interrupt: /abort fires and the frame keeps flowing.

    Pipecat's TTS/STT services react to InterruptionFrame downstream, so we
    must continue propagating it after we run our own cleanup.
    """
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/abort",
        status_code=204,
    )
    proc, pushed = _make_processor(session)

    await proc.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    aborts = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/abort")]
    assert len(aborts) == 1
    assert any(isinstance(f, InterruptionFrame) for f in pushed)


async def test_interruption_cancels_pending_ack(
    httpx_mock: HTTPXMock, session: OpencodeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-in-flight ack from the previous turn must not speak after interrupt."""
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/abort",
        status_code=204,
    )

    async def slow_ack(transcript: str) -> str:
        await asyncio.sleep(0.2)
        return f"stale: {transcript}"

    monkeypatch.setattr("friday.voice.pipecat_adapter.generate_ack", slow_ack)
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hi"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.01)
    await proc.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.3)

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


async def test_stop_speaking_pushes_interruption_without_aborting_opencode(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    """Speaker-off / mid-tail Start: silence TTS, but don't /abort the turn.

    stop_speaking is the lighter cousin of interrupt — it clears the audio
    queue downstream (InterruptionFrame to TTS + transport output) but does
    not call session.cancel(), so opencode keeps writing to the activity
    feed.
    """
    proc, pushed = _make_processor(session)

    await proc.stop_speaking()

    # InterruptionFrame went downstream so TTS clears its buffer.
    assert any(isinstance(f, InterruptionFrame) for f in pushed)
    # No /abort was issued — opencode keeps running.
    aborts = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/abort")]
    assert aborts == []


async def test_stop_speaking_cancels_pending_ack(
    httpx_mock: HTTPXMock, session: OpencodeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-in-flight ack must not speak after stop_speaking either."""
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )

    async def slow_ack(transcript: str) -> str:
        await asyncio.sleep(0.2)
        return f"stale: {transcript}"

    monkeypatch.setattr("friday.voice.pipecat_adapter.generate_ack", slow_ack)
    proc, pushed = _make_processor(session)

    await proc.process_frame(_transcription("hi"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.01)
    await proc.stop_speaking()
    await asyncio.sleep(0.3)

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


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
