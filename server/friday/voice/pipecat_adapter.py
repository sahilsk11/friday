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

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from friday.core.narration_policy import StreamingFilter, checkpoint_for_tool
from friday.core.opencode_session import OpencodeSession
from friday.core.state import AgentState

DEFAULT_ACK_TEXT = "on it"

# RTVI server-message types this processor emits. The voice room subscribes
# to these to render the live activity feed and streaming assistant text.
# See TRANSPORT.md ("How opencode events come back").
RTVI_TOOL_STARTED = "tool-started"
RTVI_ASSISTANT_TEXT_DELTA = "assistant-text-delta"
RTVI_ASSISTANT_TEXT_FINAL = "assistant-text-final"
RTVI_AGENT_STATE = "agent-state"


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
        # Strips ``` ... ``` blocks from streaming deltas. Reset between
        # responses so a turn that opens a fence and never closes (model
        # interrupted) doesn't poison the next turn.
        self._narration = StreamingFilter()

        session.on_text_delta(self._on_delta)
        session.on_text_final(self._on_final)
        session.on_state(self._on_state)
        session.on_tool_start(self._on_tool_start)

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.finalized:
            logger.info("opencode_processor: transcription -> {!r}", frame.text)
            await self._session.send_turn(frame.text)
            self._awaiting_response = True
            self._acked = False
            return

        await self.push_frame(frame, direction)

    async def _on_state(self, state: AgentState) -> None:
        # Surface the agent state to the voice-room UI on every change.
        # The pill in the activity feed reads this.
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": RTVI_AGENT_STATE, "state": state.value})
        )
        if state is AgentState.THINKING and self._awaiting_response and not self._acked:
            self._acked = True
            await self.push_frame(TTSSpeakFrame(self._ack_text))

    async def _on_delta(self, text: str) -> None:
        # Stream the *raw* delta to the UI regardless of whether it's
        # speakable text or inside a fenced code block. The UI wants to
        # render markdown faithfully; only TTS needs the StreamingFilter
        # treatment.
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": RTVI_ASSISTANT_TEXT_DELTA, "text": text})
        )

        speakable = self._narration.feed(text)
        if not speakable:
            # Inside a code fence (or held pending a fence delimiter). Don't
            # clear ack state yet — we want the immediate ack to keep firing
            # if the *visible* response is still empty.
            return
        # Real assistant text is reaching the user — suppress any not-yet-fired
        # ack and open the response bracket if needed.
        self._awaiting_response = False
        self._acked = True
        if not self._in_response:
            self._in_response = True
            await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(speakable))

    async def _on_final(self, text: str) -> None:
        self._narration.reset()
        if self._in_response:
            self._in_response = False
            await self.push_frame(LLMFullResponseEndFrame())
        # Always emit a final to the UI, even if no deltas streamed (some
        # opencode replies arrive only via MessageUpdated). The voice room
        # uses this to lock in the assistant turn in the activity feed.
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": RTVI_ASSISTANT_TEXT_FINAL, "text": text})
        )

    async def _on_tool_start(self, tool_name: str) -> None:
        # Always surface the tool start in the activity feed, even for tools
        # that have no narration phrase. The user wants to see "running grep"
        # while opencode works; the spoken narration is a separate (TTS)
        # concern with a deliberately limited vocabulary.
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": RTVI_TOOL_STARTED, "name": tool_name})
        )
        phrase = checkpoint_for_tool(tool_name)
        if phrase is None:
            return
        await self.push_frame(TTSSpeakFrame(phrase))
