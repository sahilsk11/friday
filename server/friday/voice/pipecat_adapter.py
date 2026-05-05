"""OpencodeProcessor — bridges OpencodeSession events into the pipecat pipeline.

Replaces the LLM slot in a standard pipecat voice stack:

- Consumes ``TranscriptionFrame(finalized=True)`` → ``POST /sessions/:id/turn``
  (forwarded to opencode immediately; opencode queues if a turn is in-flight).
  In parallel, kicks off a contextual ack via OpenRouter → ``TTSSpeakFrame``.
- Consumes ``InterruptionFrame`` → aborts the in-flight opencode turn and
  resets local streaming state. The frame itself is also pushed downstream
  by the base class so TTS/STT clear their own buffers. The user triggers
  this via the explicit Interrupt button (``client_message: "interrupt"``);
  there's no upstream VAD, so coughs and background noise can't fire it.
- Emits ``LLMFullResponseStartFrame`` → ``LLMTextFrame*`` → ``LLMFullResponseEndFrame``
  for each opencode response, matching the contract the assistant aggregator
  and TTS service expect.

Ack timing: fires from ``TranscriptionFrame(finalized=True)`` — the input-side
boundary where we know the user's intent — rather than from ``session.status:
busy``. The old hook required opencode to receive + dequeue + emit "busy"
before we'd say anything, which broke when opencode was already busy with
a queued turn (no fresh "busy" event ever arrives). Now: the ack races the
prompt round-trip and is suppressed by ``_acked`` if real text starts first.

Concurrency note: observer callbacks fire from the SSE loop's task and can
race with ``process_frame``. We always go out via ``self.push_frame()`` which
is queue-safe inside pipecat. Background work (ack generation, tool narration)
runs as ``asyncio.create_task`` so the LLM round-trip never blocks the frame
loop or the SSE consumer.
"""

from __future__ import annotations

import asyncio
from typing import Any, override

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from friday.core.ack_generator import generate_ack
from friday.core.narration_policy import StreamingFilter
from friday.core.opencode_session import SYSTEM_PROMPT_VOICE, ModelChoice, OpencodeSession
from friday.core.state import AgentState
from friday.core.tool_narrator import describe_tool

# RTVI server-message types this processor emits. The voice room subscribes
# to these to render the live activity feed and streaming assistant text.
# See TRANSPORT.md ("How opencode events come back").
RTVI_TOOL_STARTED = "tool-started"
RTVI_ASSISTANT_TEXT_DELTA = "assistant-text-delta"
RTVI_ASSISTANT_TEXT_FINAL = "assistant-text-final"
RTVI_AGENT_STATE = "agent-state"


class OpencodeProcessor(FrameProcessor):
    """Pipecat FrameProcessor wrapping a single OpencodeSession."""

    def __init__(
        self, session: OpencodeSession, *, system_prompt: str = SYSTEM_PROMPT_VOICE
    ) -> None:
        super().__init__()
        self._session = session
        # Sent on every send_turn via opencode's per-turn ``system`` field.
        # Per-turn is the only injection path opencode honors (create-time
        # systemPrompt is silently dropped).
        self._system_prompt = system_prompt
        # _acked: an ack has already been spoken (or pre-empted by real text)
        # for the current turn. Set when the ack TTS frame is pushed, or in
        # ``_on_delta`` to suppress a still-in-flight ack from talking over
        # the assistant. Reset on each finalized transcription.
        self._acked = False
        # _in_response: between LLMFullResponseStartFrame and ...EndFrame.
        # Bracketing prevents unmatched End frames if a session emits a
        # final-without-deltas (shouldn't happen, but cheap insurance).
        self._in_response = False
        # Strips ``` ... ``` blocks from streaming deltas. Reset between
        # responses so a turn that opens a fence and never closes (model
        # interrupted) doesn't poison the next turn.
        self._narration = StreamingFilter()
        # Strong references to background tasks — prevents GC before the
        # event loop runs them (loop only keeps weak refs to tasks).
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Current in-flight ack task. Cancelled on a fresh transcription so
        # an ack from a stale turn can't speak over the new one.
        self._ack_task: asyncio.Task[None] | None = None
        # Model the WS handler stamped from the most recent ``end-turn``
        # client message. Consumed (and cleared) by the next finalized
        # transcription. ``None`` means "let opencode use its default."
        self.next_turn_model: ModelChoice | None = None
        # Speak each tool start out loud as it happens. Off by default —
        # tool narration is chatty and most users just want it in the
        # activity feed. The WS handler flips this from the client toggle
        # carried on ``end-turn``. Sticky across turns until changed.
        self.narrate_tools: bool = False

        session.on_text_delta(self._on_delta)
        session.on_text_final(self._on_final)
        session.on_state(self._on_state)
        session.on_tool_start(self._on_tool_start)

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            await self._handle_interruption()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame) and frame.finalized:
            logger.info("opencode_processor: transcription -> {!r}", frame.text)
            self._acked = False
            # Cancel any pending ack from the previous turn — its phrase is
            # stale now and would talk over the new turn if it landed late.
            if self._ack_task is not None and not self._ack_task.done():
                self._ack_task.cancel()
            # Spawn ack generation BEFORE awaiting send_turn so the OpenRouter
            # round-trip overlaps the prompt POST. Both run concurrently with
            # everything downstream; neither blocks the frame loop.
            self._ack_task = asyncio.create_task(self._generate_and_speak_ack(frame.text))
            self._bg_tasks.add(self._ack_task)
            self._ack_task.add_done_callback(self._bg_tasks.discard)
            # Consume the model the WS handler stamped from end-turn (if any)
            # — opencode's per-session stickiness then carries it across
            # subsequent turns until the user picks again.
            model = self.next_turn_model
            self.next_turn_model = None
            await self._session.send_turn(frame.text, model=model, system=self._system_prompt)
            return

        await self.push_frame(frame, direction)

    async def _handle_interruption(self) -> None:
        """User barged in via the Interrupt button. Abort opencode and reset.

        Opencode's ``/abort`` stops the in-flight turn; the SSE stream may
        still emit one or two trailing events for it, but ``cancel()`` clears
        the per-turn accumulator so they can't surface as a final. We also
        cancel any pending ack and clear streaming bracket state so the next
        ``send_turn`` starts fresh.
        """
        logger.info("opencode_processor: interruption — aborting opencode")
        if self._ack_task is not None and not self._ack_task.done():
            self._ack_task.cancel()
        self._acked = False
        self._in_response = False
        self._narration.reset()
        try:
            await self._session.cancel()
        except Exception:
            logger.exception("opencode_processor: cancel failed")

    async def _on_state(self, state: AgentState) -> None:
        # Surface the agent state to the voice-room UI on every change.
        # The pill in the activity feed reads this. The ack itself fires from
        # ``process_frame`` on the user's transcript — not from here — so the
        # state update is purely a UI signal.
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": RTVI_AGENT_STATE, "state": state.value})
        )

    async def _generate_and_speak_ack(self, transcript: str) -> None:
        """Generate a contextual ack and speak it, unless suppressed.

        Suppression: if real assistant text has already started streaming by
        the time the OpenRouter call returns, ``_on_delta`` will have set
        ``_acked = True`` and we drop the phrase rather than talk over the
        actual response. ``CancelledError`` propagates naturally — the next
        transcript's ack replaces this one.
        """
        phrase = await generate_ack(transcript)
        if self._acked:
            logger.debug("opencode_processor: ack suppressed | phrase={!r}", phrase)
            return
        self._acked = True
        logger.info("opencode_processor: speaking ack | phrase={!r}", phrase)
        await self.push_frame(TTSSpeakFrame(phrase))

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
            # set _acked yet — we want a still-in-flight ack to fire if the
            # *visible* response so far is still empty.
            return
        # Real assistant text is reaching the user — suppress any in-flight
        # ack and open the response bracket if needed.
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

    async def _on_tool_start(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        # Spawn narration as a background task so the SSE loop isn't blocked
        # waiting for the OpenRouter HTTP call. Hold a strong ref so the event
        # loop's weak ref doesn't let the task get GC'd before it runs.
        task = asyncio.create_task(self._narrate_tool(tool_name, tool_input))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(
            lambda t: (
                logger.error(
                    "opencode_processor: narrate_tool failed | tool={} err={}",
                    tool_name,
                    t.exception(),
                )
                if not t.cancelled() and t.exception()
                else None
            )
        )

    async def _narrate_tool(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        rtvi_data: dict[str, object] = {"type": RTVI_TOOL_STARTED, "name": tool_name}
        if not self.narrate_tools:
            # UI still gets the tool start (activity feed falls back to the
            # raw name); skip the OpenRouter label call and the TTS frame.
            await self.push_frame(RTVIServerMessageFrame(data=rtvi_data))
            return
        label = await describe_tool(tool_name, tool_input)
        logger.debug("opencode_processor: narrate_tool | tool={} label={!r}", tool_name, label)
        if label:
            rtvi_data["label"] = label
        await self.push_frame(RTVIServerMessageFrame(data=rtvi_data))
        if label:
            await self.push_frame(TTSSpeakFrame(label))
