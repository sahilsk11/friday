"""Friday voice processors for bridging ProviderSession into pipecat.

``UserTranscriptMirror`` preserves Friday's custom UI transcript messages while
Pipecat owns turn detection.

``ProviderSessionProcessor`` replaces the LLM slot in a standard pipecat voice
stack:

- Receives completed user turns via ``send_user_turn`` → forwards to the
  provider via ``session.send_turn``. Turn ownership lives in Pipecat's user
  turn aggregator, not in raw STT frames.
- Consumes ``InterruptionFrame`` → calls ``session.cancel()`` to abort the
  in-flight turn and resets local streaming state. The frame is also pushed
  downstream by the base class so TTS/STT clear their own buffers. The user
  triggers this via the explicit Interrupt button (``client_message:
  "interrupt"``); there's no upstream VAD, so coughs and background noise
  can't fire it.
- Emits ``LLMFullResponseStartFrame`` → ``LLMTextFrame*`` →
  ``LLMFullResponseEndFrame`` for each provider response, matching the
  contract the assistant aggregator and TTS service expect.

Concurrency note: observer callbacks fire from the provider's event loop
(opencode's SSE task, claude-code's query() generator, …) and can race with
``process_frame``. We always go out via ``self.push_frame()`` which is
queue-safe inside pipecat. Background work (tool narration) runs as
``asyncio.create_task`` so the OpenRouter call never blocks the frame loop
or the provider's event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, override

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from friday.core.narration_policy import StreamingFilter
from friday.core.opencode_provider import SYSTEM_PROMPT_VOICE
from friday.core.provider import ModelChoice, ProviderSession, Unsubscribe
from friday.core.state import AgentState
from friday.core.tool_narrator import describe_tool

# RTVI server-message types this processor emits. The voice room subscribes
# to these to render the live activity feed and streaming assistant text.
# See TRANSPORT.md ("How opencode events come back").
RTVI_TOOL_STARTED = "tool-started"
RTVI_ASSISTANT_TEXT_DELTA = "assistant-text-delta"
RTVI_ASSISTANT_TEXT_FINAL = "assistant-text-final"
RTVI_AGENT_STATE = "agent-state"
RTVI_USER_TRANSCRIPT_RUNNING = "user-transcript-running"
RTVI_USER_TRANSCRIPT_FINAL = "user-transcript-final"


class UserTranscriptMirror(FrameProcessor):
    """Mirror STT text into Friday's custom RTVI transcript messages.

    This processor does not decide when a user turn is done. It only keeps the
    visible transcript overlay current until Pipecat's turn aggregator emits a
    completed user turn.
    """

    def __init__(self) -> None:
        super().__init__()
        self._committed: list[str] = []

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            had_text = bool(self._committed)
            self.reset()
            if had_text:
                await self.push_frame(
                    RTVIServerMessageFrame(
                        data={"type": RTVI_USER_TRANSCRIPT_RUNNING, "text": ""}
                    )
                )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if text:
                self._committed.append(text)
                await self._emit_running()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterimTranscriptionFrame):
            text = self.running_text(frame.text.strip())
            if text:
                await self.push_frame(
                    RTVIServerMessageFrame(
                        data={"type": RTVI_USER_TRANSCRIPT_RUNNING, "text": text}
                    )
                )
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    def reset(self) -> None:
        self._committed = []

    def running_text(self, tail: str = "") -> str:
        parts = [*self._committed]
        if tail:
            parts.append(tail)
        return " ".join(parts).strip()

    async def emit_final(self, text: str) -> None:
        self.reset()
        await self.push_frame(
            RTVIServerMessageFrame(
                data={"type": RTVI_USER_TRANSCRIPT_FINAL, "text": text}
            )
        )

    async def _emit_running(self) -> None:
        text = self.running_text()
        await self.push_frame(
            RTVIServerMessageFrame(
                data={"type": RTVI_USER_TRANSCRIPT_RUNNING, "text": text}
            )
        )


class ProviderSessionProcessor(FrameProcessor):
    """Pipecat FrameProcessor wrapping a single ProviderSession."""

    def __init__(
        self, session: ProviderSession, *, system_prompt: str = SYSTEM_PROMPT_VOICE
    ) -> None:
        super().__init__()
        self._session = session
        # Sent on every send_turn via the provider's per-turn ``system``
        # field. (Opencode silently drops a create-time systemPrompt; the
        # per-turn path is the only one it honors. Other providers map this
        # onto whatever their SDK exposes — see ClaudeCodeSession for the
        # SystemPromptPreset(append=...) translation.)
        self._system_prompt = system_prompt
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
        # Model the WS handler stamped from the most recent ``end-turn``
        # client message. Consumed (and cleared) by the next finalized
        # transcription. ``None`` means "let opencode use its default."
        self.next_turn_model: ModelChoice | None = None
        # Speak each tool start out loud as it happens. Off by default —
        # tool narration is chatty and most users just want it in the
        # activity feed. The WS handler flips this from the client toggle
        # carried on ``end-turn``. Sticky across turns until changed.
        self.narrate_tools: bool = False
        # Master TTS gate. Off by default so a fresh page load (or a
        # mid-turn refresh) doesn't suddenly start talking — the user
        # opts in via the speaker toggle, which sends ``set-tts`` over
        # RTVI. When False we skip tool narration TTS, and don't push
        # LLM frames into the TTS service.
        self.tts_enabled: bool = False

        # Hold the unsubscribe handles so cleanup() can detach this
        # processor's callbacks from the provider session. Sessions can
        # outlive a pipeline (opencode caches them; ClaudeCode could too),
        # so without these, every reconnect would leak dead handlers that
        # keep firing into a torn-down pipeline.
        self._unsubscribes: list[Unsubscribe] = [
            session.on_text_delta(self._on_delta),
            session.on_text_final(self._on_final),
            session.on_state(self._on_state),
            session.on_tool_start(self._on_tool_start),
        ]

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            await self.cancel_current_turn()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def send_user_turn(self, text: str) -> None:
        """Forward a completed Pipecat-owned user turn to the provider."""
        text = text.strip()
        if not text:
            return
        logger.info("provider_session_processor: user turn -> {!r}", text)
        # Consume the model the WS handler stamped from end-turn (if any) —
        # opencode's per-session stickiness then carries it across subsequent
        # turns until the user picks again.
        model = self.next_turn_model
        self.next_turn_model = None
        await self._session.send_turn(text, model=model, system=self._system_prompt)

    async def cancel_current_turn(self) -> None:
        """Cancel the provider's in-flight turn and reset local response state."""
        await self._handle_interruption()

    @override
    async def cleanup(self) -> None:
        """Detach observers from the provider session.

        Provider sessions can outlive a pipeline (opencode caches them
        keyed by id; the SSE loop is shared globally), so handlers we
        registered in ``__init__`` would otherwise keep pushing frames
        into a torn-down processor.
        """
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes.clear()
        await super().cleanup()

    async def _handle_interruption(self) -> None:
        """User barged in via the Interrupt button. Cancel the turn and reset.

        ``session.cancel()`` is provider-defined: opencode POSTs ``/abort``
        and clears its per-turn accumulator so trailing SSE events for the
        cancelled turn can't surface as a final; ClaudeCode cancels the
        in-flight ``query()`` task. Either way we also clear streaming
        bracket state so the next ``send_turn`` starts fresh.
        """
        logger.info("provider_session_processor: interruption — cancelling turn")
        self._in_response = False
        self._narration.reset()
        try:
            await self._session.cancel()
        except Exception:
            logger.exception("provider_session_processor: cancel failed")

    async def stop_speaking(self) -> None:
        """Silence TTS without aborting opencode.

        Pushes ``InterruptionFrame`` downstream from this processor so TTS
        cancels in-flight synthesis and ``transport.output()`` drops queued
        audio. Opencode keeps streaming text to the activity feed; subsequent
        deltas arriving while ``tts_enabled`` is still True will resume
        speaking. Caller pairs this with ``set-tts off`` for a permanent mute.

        Used by:
          - Speaker-off toggle (alongside ``set-tts``) — drains audio that
            was already synthesized before the flag flipped.
          - Start (mic on) when opencode is no longer thinking but TTS is
            still draining its tail. ``InterruptionFrame`` upstream would
            also fire ``cancel()`` on opencode, which we don't want here.
        """
        logger.info("provider_session_processor: stop-speaking")
        await self.push_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    async def _on_state(self, state: AgentState) -> None:
        # Surface the agent state to the voice-room UI on every change.
        # The pill in the activity feed reads this.
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": RTVI_AGENT_STATE, "state": state.value})
        )

    async def _on_delta(self, text: str) -> None:
        # Stream the *raw* delta to the UI regardless of whether it's
        # speakable text or inside a fenced code block. The UI wants to
        # render markdown faithfully; only TTS needs the StreamingFilter
        # treatment.
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": RTVI_ASSISTANT_TEXT_DELTA, "text": text})
        )

        if not self.tts_enabled:
            # UI already got the delta above; nothing else needs to flow.
            return

        speakable = self._narration.feed(text)
        if not speakable:
            # Inside a code fence (or held pending a fence delimiter).
            return
        # Real assistant text is reaching the user — open the response
        # bracket if needed.
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
                    "provider_session_processor: narrate_tool failed | tool={} err={}",
                    tool_name,
                    t.exception(),
                )
                if not t.cancelled() and t.exception()
                else None
            )
        )

    async def _narrate_tool(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        rtvi_data: dict[str, object] = {"type": RTVI_TOOL_STARTED, "name": tool_name}
        if not self.narrate_tools or not self.tts_enabled:
            # UI still gets the tool start (activity feed falls back to the
            # raw name); skip the OpenRouter label call and the TTS frame.
            await self.push_frame(RTVIServerMessageFrame(data=rtvi_data))
            return
        label = await describe_tool(tool_name, tool_input)
        logger.debug(
            "provider_session_processor: narrate_tool | tool={} label={!r}", tool_name, label
        )
        if label:
            rtvi_data["label"] = label
        await self.push_frame(RTVIServerMessageFrame(data=rtvi_data))
        if label:
            await self.push_frame(TTSSpeakFrame(label))
