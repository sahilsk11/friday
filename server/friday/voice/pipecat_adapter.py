"""OpencodeProcessor — bridges OpencodeSession events into the pipecat pipeline.

Replaces the LLM slot in a standard pipecat voice stack:

- Consumes ``TranscriptionFrame(finalized=True)`` → ``POST /sessions/:id/turn``
  (forwarded to opencode immediately; opencode queues if a turn is in-flight).
- Consumes ``InterruptionFrame`` → ignored. v1 lets opencode drain its queue
  rather than calling ``/abort`` on every barge-in.
- Emits ``LLMFullResponseStartFrame`` → ``LLMTextFrame*`` → ``LLMFullResponseEndFrame``
  for each opencode response, matching the contract the assistant aggregator
  and TTS service expect.
- Emits ``TTSSpeakFrame("on it")`` once per turn — the immediate ack — when
  opencode goes ``busy`` before any text deltas arrive. Suppressed if real
  text has already started streaming.

Concurrency note: observer callbacks fire from the SSE loop's task and can
race with ``process_frame``. We always go out via ``self.push_frame()`` which
is queue-safe inside pipecat. We use ``self.create_task()`` for any async
work spawned from observer paths so pipecat's ``TaskManager`` cleans up.
"""

from __future__ import annotations

from typing import override

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from friday.core.opencode_session import OpencodeSession
from friday.core.state import AgentState

DEFAULT_ACK_TEXT = "on it"


class OpencodeProcessor(FrameProcessor):
    """Pipecat FrameProcessor wrapping a single OpencodeSession."""

    def __init__(self, session: OpencodeSession, *, ack_text: str = DEFAULT_ACK_TEXT) -> None:
        super().__init__()
        self._session = session
        self._ack_text = ack_text
        # _awaiting_response: a turn was sent and we haven't seen its first
        # delta or final yet. Cleared on first delta of the response.
        self._awaiting_response = False
        # _acked: ack already fired for the current pending turn. Reset on
        # send_turn. Independent of _awaiting_response so duplicate
        # ``session.status:busy`` events don't double-ack.
        self._acked = False
        # _in_response: between LLMFullResponseStartFrame and ...EndFrame.
        # Bracketing prevents unmatched End frames if a session emits a
        # final-without-deltas (shouldn't happen, but cheap insurance).
        self._in_response = False

        session.on_text_delta(self._on_delta)
        session.on_text_final(self._on_final)
        session.on_state(self._on_state)

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.finalized:
            await self._session.send_turn(frame.text)
            self._awaiting_response = True
            self._acked = False
            return

        await self.push_frame(frame, direction)

    async def _on_state(self, state: AgentState) -> None:
        if state is AgentState.THINKING and self._awaiting_response and not self._acked:
            self._acked = True
            await self.push_frame(TTSSpeakFrame(self._ack_text))

    async def _on_delta(self, text: str) -> None:
        # Real assistant text is starting — suppress any not-yet-fired ack
        # and open the response bracket if needed.
        self._awaiting_response = False
        self._acked = True
        if not self._in_response:
            self._in_response = True
            await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(text))

    async def _on_final(self, _text: str) -> None:
        if self._in_response:
            self._in_response = False
            await self.push_frame(LLMFullResponseEndFrame())
