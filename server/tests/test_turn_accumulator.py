"""Tests for TurnAccumulator.

Black-box: feed pipecat frames into ``process_frame`` (and call
``arm_flush()``), capture pushed frames via a ``push_frame`` override,
assert on the final ``TranscriptionFrame`` plus the running/final RTVI
server messages.

Same lifecycle shortcut as ``test_pipecat_adapter.py``: no Pipeline,
no StartFrame — just exercise the processor directly.
"""

from __future__ import annotations

import asyncio

import pytest
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from friday.voice.turn_accumulator import (
    RTVI_USER_TRANSCRIPT_FINAL,
    RTVI_USER_TRANSCRIPT_RUNNING,
    TurnAccumulator,
)


def _make_accumulator(
    *, silence_secs: float = 0.05, pending_commit_timeout_secs: float = 0.05
) -> tuple[TurnAccumulator, list[Frame]]:
    """Build the accumulator with short timers so tests run in real time.

    Defaults shrink ``silence_secs`` and ``pending_commit_timeout_secs``
    from production's 3.0/1.5 down to 50ms each — enough for asyncio to
    schedule a sleep, fast enough that a full-suite run isn't slow.
    """
    pushed: list[Frame] = []
    acc = TurnAccumulator(
        silence_secs=silence_secs,
        pending_commit_timeout_secs=pending_commit_timeout_secs,
    )

    async def capture(frame: Frame, _direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append(frame)

    acc.push_frame = capture  # type: ignore[method-assign]
    return acc, pushed


def _commit(text: str, *, finalized: bool = False) -> TranscriptionFrame:
    """A TranscriptionFrame as the STT layer produces in VAD mode (default
    ``finalized=False``). The accumulator should treat both values the
    same — buffer it, regardless."""
    return TranscriptionFrame(
        text=text,
        user_id="u1",
        timestamp="2026-01-01T00:00:00Z",
        finalized=finalized,
    )


def _running_messages(pushed: list[Frame]) -> list[str]:
    out: list[str] = []
    for f in pushed:
        if isinstance(f, RTVIServerMessageFrame):
            data = f.data
            if isinstance(data, dict) and data.get("type") == RTVI_USER_TRANSCRIPT_RUNNING:
                text = data.get("text")
                if isinstance(text, str):
                    out.append(text)
    return out


def _final_messages(pushed: list[Frame]) -> list[str]:
    out: list[str] = []
    for f in pushed:
        if isinstance(f, RTVIServerMessageFrame):
            data = f.data
            if isinstance(data, dict) and data.get("type") == RTVI_USER_TRANSCRIPT_FINAL:
                text = data.get("text")
                if isinstance(text, str):
                    out.append(text)
    return out


def _final_transcriptions(pushed: list[Frame]) -> list[TranscriptionFrame]:
    return [f for f in pushed if isinstance(f, TranscriptionFrame) and f.finalized]


async def test_buffers_commits_and_emits_running_messages() -> None:
    """Each commit appends to the buffer and emits a running RTVI message
    with the cumulative text. No finalized frame yet — the turn is still
    in progress."""
    acc, pushed = _make_accumulator()

    await acc.process_frame(_commit("hello"), FrameDirection.DOWNSTREAM)
    await acc.process_frame(_commit("world"), FrameDirection.DOWNSTREAM)

    assert _running_messages(pushed) == ["hello", "hello world"]
    assert _final_transcriptions(pushed) == []
    assert _final_messages(pushed) == []


async def test_silence_timer_flushes_consolidated_turn() -> None:
    """After ``silence_secs`` of no commits, the accumulator emits one
    finalized TranscriptionFrame plus a final RTVI message with the
    consolidated text."""
    acc, pushed = _make_accumulator(silence_secs=0.05)

    await acc.process_frame(_commit("hello"), FrameDirection.DOWNSTREAM)
    await acc.process_frame(_commit("world"), FrameDirection.DOWNSTREAM)

    # Wait past the silence window. A small extra margin avoids races with
    # the asyncio scheduler — the timer task needs to actually run.
    await asyncio.sleep(0.12)

    finals = _final_transcriptions(pushed)
    assert len(finals) == 1
    assert finals[0].text == "hello world"
    assert finals[0].finalized is True
    assert _final_messages(pushed) == ["hello world"]


async def test_arm_flush_fires_on_next_commit() -> None:
    """Tap-to-send arms the accumulator. When the next commit arrives
    (forced by the upstream VADUserStoppedSpeakingFrame → STT shim path),
    the buffer flushes immediately, not after the silence window."""
    acc, pushed = _make_accumulator(silence_secs=10.0, pending_commit_timeout_secs=10.0)

    await acc.process_frame(_commit("hello"), FrameDirection.DOWNSTREAM)
    acc.arm_flush()
    await acc.process_frame(_commit("there"), FrameDirection.DOWNSTREAM)

    finals = _final_transcriptions(pushed)
    assert len(finals) == 1
    assert finals[0].text == "hello there"


async def test_arm_flush_falls_back_to_timeout_when_no_commit_arrives() -> None:
    """If the user taps Send but the manual commit's committed_transcript
    never lands (network glitch, ElevenLabs stalled), we shouldn't strand
    the buffer. After ``pending_commit_timeout_secs`` we flush whatever
    we have."""
    acc, pushed = _make_accumulator(silence_secs=10.0, pending_commit_timeout_secs=0.05)

    await acc.process_frame(_commit("hello"), FrameDirection.DOWNSTREAM)
    acc.arm_flush()

    await asyncio.sleep(0.12)

    finals = _final_transcriptions(pushed)
    assert len(finals) == 1
    assert finals[0].text == "hello"


async def test_interruption_clears_buffer_and_passes_through() -> None:
    """Interrupt = abandon the in-flight turn. Buffer drops, no flush
    fires, the InterruptionFrame still flows downstream so TTS/STT/
    ProviderSessionProcessor can clear their own state, and we emit an empty
    running message so the client UI overlay clears (otherwise the last
    running text would linger until the next turn)."""
    acc, pushed = _make_accumulator(silence_secs=10.0)

    await acc.process_frame(_commit("hello"), FrameDirection.DOWNSTREAM)
    interrupt = InterruptionFrame()
    await acc.process_frame(interrupt, FrameDirection.DOWNSTREAM)

    # Subsequent silence shouldn't fire a flush — buffer was cleared.
    await asyncio.sleep(0.05)

    assert any(f is interrupt for f in pushed)
    assert _final_transcriptions(pushed) == []
    assert _final_messages(pushed) == []
    # Running messages: "hello" while accumulating, then "" on interrupt.
    assert _running_messages(pushed) == ["hello", ""]


async def test_interim_transcription_passes_through() -> None:
    """Per-word partials shouldn't enter the buffer (commits do); they
    just pass through unchanged for any consumer that wants them."""
    acc, pushed = _make_accumulator()

    interim = InterimTranscriptionFrame(
        text="he",
        user_id="u1",
        timestamp="2026-01-01T00:00:00Z",
    )
    await acc.process_frame(interim, FrameDirection.DOWNSTREAM)

    assert any(f is interim for f in pushed)
    assert _running_messages(pushed) == []  # no commit landed yet


async def test_handles_finalized_true_input_same_as_finalized_false() -> None:
    """The input ``finalized`` flag is irrelevant — a commit is a commit.
    Both VAD-mode (finalized=False) and MANUAL-mode-90s-overflow
    (finalized=True) inputs must accumulate the same way. We synthesize
    our own finalized=True on flush."""
    acc, pushed = _make_accumulator(silence_secs=0.05)

    await acc.process_frame(_commit("hello", finalized=False), FrameDirection.DOWNSTREAM)
    await acc.process_frame(_commit("world", finalized=True), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.12)

    finals = _final_transcriptions(pushed)
    assert len(finals) == 1
    assert finals[0].text == "hello world"


async def test_silence_timer_resets_on_each_commit() -> None:
    """A commit during the silence window cancels the in-flight timer
    and starts a fresh one. The buffer accumulates and only flushes
    after a real silence window with no new commits."""
    acc, pushed = _make_accumulator(silence_secs=0.08)

    await acc.process_frame(_commit("a"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.04)  # half the silence window
    await acc.process_frame(_commit("b"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.04)
    await acc.process_frame(_commit("c"), FrameDirection.DOWNSTREAM)
    # No flush yet — each commit reset the timer.
    assert _final_transcriptions(pushed) == []
    await asyncio.sleep(0.12)

    finals = _final_transcriptions(pushed)
    assert len(finals) == 1
    assert finals[0].text == "a b c"


async def test_buffer_resets_after_flush_for_next_turn() -> None:
    """Flushing one turn must leave the accumulator ready to start a
    fresh buffer for the next turn — no stale fragments leaking across
    turn boundaries."""
    acc, pushed = _make_accumulator(silence_secs=0.05)

    await acc.process_frame(_commit("first turn"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.12)
    pushed.clear()  # Drop turn-1 frames so we can assert turn-2 in isolation.

    await acc.process_frame(_commit("second turn"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.12)

    finals = _final_transcriptions(pushed)
    assert len(finals) == 1
    assert finals[0].text == "second turn"


async def test_empty_commits_dont_flush_or_emit_running() -> None:
    """An empty committed_transcript (no audible speech in the segment)
    is a no-op — the buffer doesn't change, no running message goes out
    to blank the live UI, and the silence timer still resets so we
    don't immediately flush an empty turn."""
    acc, pushed = _make_accumulator(silence_secs=0.05)

    await acc.process_frame(_commit(""), FrameDirection.DOWNSTREAM)
    # Nothing visible.
    assert _running_messages(pushed) == []
    # And after the silence window, no final either (buffer is empty).
    await asyncio.sleep(0.12)
    assert _final_transcriptions(pushed) == []
    assert _final_messages(pushed) == []


@pytest.mark.parametrize("text", ["  hello  ", "hello"])
async def test_commit_text_is_stripped(text: str) -> None:
    """ElevenLabs commits sometimes include trailing whitespace; the
    accumulator strips on the way in so " ".join() doesn't produce
    double spaces."""
    acc, pushed = _make_accumulator(silence_secs=0.05)

    await acc.process_frame(_commit(text), FrameDirection.DOWNSTREAM)
    await acc.process_frame(_commit("world"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.12)

    finals = _final_transcriptions(pushed)
    assert len(finals) == 1
    assert finals[0].text == "hello world"
