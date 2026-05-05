"""TurnAccumulator — decouples STT segment commits from turn boundaries.

Why this exists
---------------
ElevenLabs realtime STT commits aggressively in VAD mode (every ~500ms of
silence), and even in MANUAL mode auto-commits at 90s to keep its audio
buffer bounded. Each commit becomes a ``TranscriptionFrame``. Without an
accumulator, every commit fires a turn at the downstream ``OpencodeProcessor``
— splitting one spoken thought across multiple opencode turns whenever the
user pauses, and producing a phantom turn at the 90s mark for long
utterances.

Treating commits as turn boundaries was the architectural mistake. A commit
is just "ASR finalized this audio chunk; flush the buffer." A turn is "user
finished speaking, run the agent." This processor separates the two.

Pipeline placement
------------------
Sits between STT and OpencodeProcessor::

    transport.input → stt → TurnAccumulator → opencode → tts → ...

What it does
------------
- Catches every ``TranscriptionFrame`` (any ``finalized`` value), appends the
  text to an internal buffer, and resets the silence timer.
- Emits an RTVI ``user-transcript-running`` server message on each commit so
  the live UI can render the running accumulated text (replaces, doesn't
  append).
- Flushes the buffer downstream as ONE synthetic ``TranscriptionFrame
  (finalized=True)`` plus an RTVI ``user-transcript-final`` server message
  when the turn really ends:
    * ``arm_flush()`` was called (tap-to-send) and the next commit lands —
      with a timeout fallback if the commit never arrives.
    * The silence timer fires (no commits for ``silence_secs``).
- Clears its buffer on ``InterruptionFrame`` and passes the frame through.
- Passes ``InterimTranscriptionFrame`` (per-word partials, finer-grained
  than commits) through unchanged for any consumer that wants them.

RTVI message contract
---------------------
Two custom server-message types this processor emits, both consumed by the
frontend in place of pipecat's built-in user-transcript RTVI auto-emit
(which is disabled at the pipeline level — see ``server.py``)::

    {"type": "user-transcript-running", "text": "<accumulated so far>"}
    {"type": "user-transcript-final",   "text": "<consolidated final>"}

The running message replaces the live transcript display on every commit.
The final message is the lock-in signal: the activity feed appends one
entry, and the same text lands at ``OpencodeProcessor`` as the synthetic
finalized ``TranscriptionFrame``.
"""

from __future__ import annotations

import asyncio
from typing import override

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

# RTVI server-message types this processor emits. The voice room subscribes
# to these in place of the built-in RTVI ``UserTranscript`` event.
RTVI_USER_TRANSCRIPT_RUNNING = "user-transcript-running"
RTVI_USER_TRANSCRIPT_FINAL = "user-transcript-final"

# Time-since-last-commit threshold for the auto-flush turn-end signal. With
# ElevenLabs VAD at 500ms, "no commits for 3s" means the user has been
# silent for at least ~2.5s past the last natural pause — long enough to
# infer the turn is over without false-positive mid-sentence flushes.
DEFAULT_SILENCE_SECS = 3.0
# How long we wait for the manual commit's ``committed_transcript`` after
# tap-to-send arms a flush. If ElevenLabs is slow or the commit gets lost,
# we flush whatever's already buffered rather than stranding the user's
# turn.
DEFAULT_PENDING_COMMIT_TIMEOUT_SECS = 1.5


class TurnAccumulator(FrameProcessor):
    """Buffer STT commits; emit one finalized turn per real user turn."""

    def __init__(
        self,
        *,
        silence_secs: float = DEFAULT_SILENCE_SECS,
        pending_commit_timeout_secs: float = DEFAULT_PENDING_COMMIT_TIMEOUT_SECS,
    ) -> None:
        super().__init__()
        self._silence_secs = silence_secs
        self._pending_commit_timeout_secs = pending_commit_timeout_secs

        # Accumulated text fragments for the in-progress turn. Each fragment
        # is a single committed_transcript from ElevenLabs.
        self._buffer: list[str] = []
        # Sticky across the in-progress turn so the synthetic flush frame
        # carries the same identity as the underlying commits.
        self._language: Language | None = None
        self._user_id: str = ""

        # Background task that fires _silence_flush after _silence_secs of
        # no new commits. Cancelled and recreated on each new commit; only
        # fires when the buffer has been quiet long enough.
        self._silence_task: asyncio.Task[None] | None = None

        # Pending-commit guard: when armed (e.g. via tap-to-send), the next
        # committed fragment triggers an immediate flush. The timeout task
        # is the safety net that flushes whatever's buffered if the commit
        # never arrives.
        self._armed: bool = False
        self._arm_timeout_task: asyncio.Task[None] | None = None

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            # User barge-in: drop the buffer; the in-flight turn is being
            # cancelled, so any half-finalized fragments are stale. Emit a
            # running message with empty text so the live UI overlay clears
            # — without it, the previous turn's last running text would
            # linger until the next turn starts. The frame itself flows
            # through to clear downstream TTS/STT state and trigger
            # OpencodeProcessor's abort path.
            had_buffer = bool(self._buffer)
            self._reset()
            if had_buffer:
                await self.push_frame(
                    RTVIServerMessageFrame(
                        data={"type": RTVI_USER_TRANSCRIPT_RUNNING, "text": ""}
                    )
                )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterimTranscriptionFrame):
            # Per-word partials. Forward unchanged — anyone listening for
            # them gets them, and they never enter the buffer (commits do).
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            await self._on_committed(frame)
            return

        await self.push_frame(frame, direction)

    def arm_flush(self) -> None:
        """Flush as soon as the next ``TranscriptionFrame`` arrives.

        Used when the client taps end-turn: the WS handler forges
        ``VADUserStoppedSpeakingFrame`` upstream which causes the STT shim
        to send a manual commit; ElevenLabs replies with a
        ``committed_transcript`` that lands here, and we flush the
        consolidated buffer plus this trailing fragment as a single turn.

        Falls back to flushing whatever is already buffered after
        ``pending_commit_timeout_secs`` if the commit never arrives.
        """
        logger.info(
            "turn_accumulator: arming flush | buffer_size={} timeout={}",
            len(self._buffer),
            self._pending_commit_timeout_secs,
        )
        self._cancel_arm_timeout()
        self._armed = True
        self._arm_timeout_task = asyncio.create_task(self._arm_timeout())

    async def _on_committed(self, frame: TranscriptionFrame) -> None:
        text = frame.text.strip()
        if text:
            self._buffer.append(text)
            self._language = frame.language or self._language
            self._user_id = frame.user_id or self._user_id

        # Live UI: replace whatever it's showing with the running buffer.
        # Empty commits (rare — no audible speech in the segment) are
        # filtered above so we don't blank the overlay on a no-op commit.
        if text:
            await self._emit_running()

        # Reset silence timer regardless — even an empty commit means
        # something arrived.
        self._reset_silence_timer()

        if self._armed:
            logger.info(
                "turn_accumulator: armed flush firing on commit | buffer_size={}",
                len(self._buffer),
            )
            self._armed = False
            self._cancel_arm_timeout()
            await self._flush()

    async def _arm_timeout(self) -> None:
        try:
            await asyncio.sleep(self._pending_commit_timeout_secs)
        except asyncio.CancelledError:
            return
        if not self._armed:
            return
        logger.info(
            "turn_accumulator: armed flush timed out — flushing buffer | size={}",
            len(self._buffer),
        )
        self._armed = False
        await self._flush()

    def _reset_silence_timer(self) -> None:
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = asyncio.create_task(self._silence_flush())

    async def _silence_flush(self) -> None:
        try:
            await asyncio.sleep(self._silence_secs)
        except asyncio.CancelledError:
            return
        if not self._buffer:
            return
        logger.info(
            "turn_accumulator: silence flush | size={} silence_secs={}",
            len(self._buffer),
            self._silence_secs,
        )
        await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return

        text = " ".join(self._buffer).strip()
        language = self._language
        user_id = self._user_id

        # Reset state BEFORE emitting frames so any re-entrancy from
        # downstream observers can't double-flush.
        self._buffer = []
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = None

        # Final RTVI message — frontend ActivityFeed locks this into the
        # feed as a user turn.
        await self.push_frame(
            RTVIServerMessageFrame(
                data={"type": RTVI_USER_TRANSCRIPT_FINAL, "text": text}
            )
        )
        # Synthetic finalized transcription downstream — OpencodeProcessor
        # consumes this exactly like it would consume an ElevenLabs
        # committed_transcript in MANUAL mode.
        await self.push_frame(
            TranscriptionFrame(
                text=text,
                user_id=user_id,
                timestamp=time_now_iso8601(),
                language=language,
                finalized=True,
            )
        )

    async def _emit_running(self) -> None:
        running = " ".join(self._buffer).strip()
        await self.push_frame(
            RTVIServerMessageFrame(
                data={"type": RTVI_USER_TRANSCRIPT_RUNNING, "text": running}
            )
        )

    def _reset(self) -> None:
        self._buffer = []
        self._armed = False
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = None
        self._cancel_arm_timeout()

    def _cancel_arm_timeout(self) -> None:
        if self._arm_timeout_task is not None and not self._arm_timeout_task.done():
            self._arm_timeout_task.cancel()
        self._arm_timeout_task = None

    @override
    async def cleanup(self) -> None:
        self._reset()
        await super().cleanup()
