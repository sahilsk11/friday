"""WebSocket transport + per-connection pipeline assembly.

Mounts ``WS /api/voice`` onto the same FastAPI app as the REST/SSE session
router. The browser opens a WebSocket; pipecat's ``FastAPIWebsocketTransport``
runs both directions of audio over it (PCM frames serialized via protobuf).

We previously used WebRTC (``SmallWebRTCTransport``). The handshake — ICE
candidate gathering, candidate-pair STUN checks, DTLS-SRTP — added 8-15s of
connect latency on multi-interface machines (Tailscale, virtual bridges,
IPv6) for zero benefit: friday is one user per machine, browser and server
sharing localhost in dev, sharing an origin behind Caddy in prod. WebRTC's
NAT-traversal ceremony has nothing to do here. WebSocket is sub-second.

Pipeline shape per connection::

    transport.input()
        → STT (ElevenLabs realtime VAD-mode, or Deepgram)
        → TurnAccumulator                           # buffers commits → turns
        → ProviderSessionProcessor                         # replaces the LLM slot
        → TTS (ElevenLabs, or Cartesia)
        → transport.output()
        → RTVIProcessor                             # voice-UI state surface

Why a TurnAccumulator: ElevenLabs realtime STT in VAD mode commits
aggressively (every ~500ms of silence) to keep its own audio buffer small,
and even in MANUAL mode auto-commits at 90s. Each commit is just an ASR
buffer flush, not a turn boundary — but every commit produces a
``TranscriptionFrame`` that the downstream processor would otherwise treat
as a turn. The accumulator separates the two concerns: it buffers commits
into the in-progress turn and only emits a synthetic finalized
``TranscriptionFrame`` downstream when the *real* turn ends (3s of silence
for hands-free mode, or tap-to-send via ``arm_flush``). See
``turn_accumulator.py`` for details.

Why no Silero VAD: ElevenLabs realtime STT does its own segmentation in
VAD mode at the threshold we configure (500ms). A second VAD on top would
fight ElevenLabs' commit timing. If we ever swap to a STT without built-in
VAD, add Silero here.

Notes:

- App data (sessions, transcripts, agent state) flows through REST/SSE in
  ``friday.api.sessions`` — not via RTVI custom messages. RTVI is voice-UI
  state only.
- ``?session_id=…`` on the WS URL attaches to an existing opencode session;
  if absent, we create a new one.
- Auth (Step 6) will accept ``?t=…`` here once it lands; until then the
  endpoint is open and expects to be reachable on localhost.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, WebSocket
from loguru import logger
from pipecat.frames.frames import InterruptionTaskFrame, VADUserStoppedSpeakingFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.models import ClientMessage
from pipecat.processors.frameworks.rtvi.observer import RTVIObserver, RTVIObserverParams
from pipecat.processors.frameworks.rtvi.processor import RTVIProcessor
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.stt import (
    CommitStrategy,
    ElevenLabsRealtimeSTTSettings,
)
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from friday.core.provider import ModelChoice
from friday.core.session_manager import SessionManager
from friday.voice.elevenlabs_force_commit import ElevenLabsRealtimeSTTServiceForceCommit
from friday.voice.pipecat_adapter import ProviderSessionProcessor
from friday.voice.turn_accumulator import TurnAccumulator

router = APIRouter(tags=["voice"])


# We can't use the HTTP-flavored ``get_manager`` Depends from
# ``friday.api.sessions`` here — FastAPI's WebSocket scope doesn't have a
# ``Request`` to inject. Read straight off ``app.state`` instead.
def _resolve_manager(websocket: WebSocket) -> SessionManager:
    manager: SessionManager | None = getattr(websocket.app.state, "manager", None)
    if manager is None:
        raise RuntimeError("session manager not ready")
    return manager


# Sample rates match the @pipecat-ai/websocket-transport client defaults
# (recorderSampleRate=16000 for mic input, playerSampleRate=24000 for TTS
# output). Keeping these aligned avoids resampling on either side.
_AUDIO_IN_SAMPLE_RATE = 16_000
_AUDIO_OUT_SAMPLE_RATE = 24_000


@router.websocket("/api/voice")
async def voice(websocket: WebSocket, session_id: str | None = None) -> None:
    """Bidirectional voice stream.

    Accepts ``?session_id=…`` to attach to an existing opencode session.
    Without it, lazily creates a new session.

    Returns when the client closes the WebSocket; pipecat's PipelineRunner
    tears down its pipeline and closes the underlying ``FastAPIWebsocketClient``.
    """
    manager = _resolve_manager(websocket)
    await websocket.accept()

    if session_id:
        session = manager.attach(session_id)
        logger.info("voice: attached to opencode session | id={}", session.id)
    else:
        session = await manager.create()
        logger.info("voice: created new opencode session | id={}", session.id)

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=_AUDIO_IN_SAMPLE_RATE,
            audio_out_sample_rate=_AUDIO_OUT_SAMPLE_RATE,
            add_wav_header=False,
            serializer=ProtobufFrameSerializer(),
        ),
    )

    stt = _select_stt()
    tts = _select_tts()
    accumulator = TurnAccumulator()
    agent = ProviderSessionProcessor(session)
    rtvi = RTVIProcessor(transport=transport)

    # Echo client-ready back as bot-ready. Pipecat's RTVIProcessor only
    # sends bot-ready when the app explicitly signals it — the convention
    # in WebRTC apps is to wait for a separate bot worker to load models /
    # connect to the LLM. friday's pipeline is ready the moment the WS
    # accepts the connection (STT/TTS WebSockets are dialled inside the
    # PipelineRunner before ``StartFrame`` propagates), so we ack as soon
    # as the client says hello. Without this, voice-ui-kit's ClientStatus
    # pill reads "Agent connecting" forever even though everything works.
    #
    # Right after the bot-ready ack, replay the current opencode agent
    # state so a UI that just (re)connected mid-turn shows the thinking
    # indicator immediately — opencode only fires state events on
    # transitions, so without this an in-flight turn looks idle until the
    # next change. ``send_server_message`` is the post-start, RTVI-aware
    # path; pushing an RTVIServerMessageFrame from the processor before
    # this point trips RTVIProcessor's "StartFrame not received yet" check.
    async def _on_client_ready(processor: RTVIProcessor) -> None:
        await processor.set_bot_ready()
        await processor.send_server_message(
            {"type": "agent-state", "state": session.current_state.value}
        )

    rtvi.event_handler("on_client_ready")(_on_client_ready)

    # Tap-to-end-turn: client sends {type: "end-turn"} when the user is
    # done speaking. Two things have to happen, in order:
    #   1. Arm the TurnAccumulator so it knows the next committed_transcript
    #      from ElevenLabs is the trailing edge of the turn — flush as soon
    #      as it lands (with a timeout fallback if it doesn't).
    #   2. Push VADUserStoppedSpeakingFrame upstream so the STT shim
    #      (ElevenLabsRealtimeSTTServiceForceCommit) sends {commit: True}
    #      to ElevenLabs even in VAD mode, capturing the audio between the
    #      last natural pause and the tap.
    # Hands-free turn-ends — 3s of silence with no commits arriving — are
    # handled entirely inside the accumulator; no client message needed.
    async def _on_client_message(processor: RTVIProcessor, msg: ClientMessage) -> None:
        if msg.type == "end-turn":
            # Optional ``model`` rides along on end-turn — we stamp it on the
            # ProviderSessionProcessor so the next finalized transcription forwards
            # it to opencode. No server-side stickiness; the client owns the
            # selection and re-sends it whenever it changes.
            agent.next_turn_model = _parse_model(msg.data)
            # Tool narration toggle also rides along — sticky on the
            # processor (unlike model). Client re-sends it each turn so a
            # toggle flip propagates without its own message type.
            narrate = _parse_narrate_tools(msg.data)
            if narrate is not None:
                agent.narrate_tools = narrate
            logger.info(
                "voice: end-turn received | session={} model={} narrate_tools={}",
                session.id,
                agent.next_turn_model,
                agent.narrate_tools,
            )
            accumulator.arm_flush()
            await processor.push_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        elif msg.type == "interrupt":
            # User tapped the Interrupt button. Push InterruptionTaskFrame
            # upstream — the pipeline task converts it to a downstream
            # InterruptionFrame, which clears TTS audio + STT audio buffers
            # along the way. ProviderSessionProcessor handles the same frame to
            # abort the in-flight opencode turn (see pipecat_adapter.py).
            # Send and Interrupt stay separate: interrupt = "shut up", and
            # the next turn only goes out when the user taps Send again.
            logger.info("voice: interrupt received | session={}", session.id)
            await processor.push_frame(InterruptionTaskFrame(), FrameDirection.UPSTREAM)
        elif msg.type == "stop-speaking":
            # Mute TTS without killing opencode. Used by Start (mic on) when
            # the agent is past `thinking` but TTS is still draining audio,
            # and by the speaker toggle when flipped off — both want
            # "shut up now" without aborting the in-flight turn the way
            # `interrupt` does.
            logger.info("voice: stop-speaking received | session={}", session.id)
            await agent.stop_speaking()
        elif msg.type == "set-tts":
            # Speaker toggle. Defaults to off on every fresh page load —
            # the client sends this whenever the user flips the switch
            # (and once on connect to assert the current value). When off
            # we keep the WS open and keep streaming events to the UI;
            # we just don't burn TTS synthesis or speak the assistant.
            enabled = _parse_tts_enabled(msg.data)
            if enabled is not None:
                agent.tts_enabled = enabled
                logger.info(
                    "voice: set-tts | session={} enabled={}",
                    session.id,
                    enabled,
                )

    rtvi.event_handler("on_client_message")(_on_client_message)

    # RTVI sits BEFORE transport.output() because RTVI emits its control
    # messages (bot-ready, our custom server messages, transcripts, …) by
    # pushing OutputTransportMessageUrgentFrames DOWNSTREAM. If RTVI is at
    # the end of the pipeline those frames fall off the back; the WS
    # client never sees them and ClientStatus's "Agent" half stays
    # "connecting" forever. Audio frames from TTS pass through RTVI
    # unchanged on their way to transport.output().
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            accumulator,
            agent,
            tts,
            rtvi,
            transport.output(),
        ]
    )

    # Disable the observer's built-in user-transcription auto-emit. The
    # accumulator owns user-transcript messaging now via two custom RTVI
    # server-message types (running and final); without this flag every
    # commit from ElevenLabs would still fan out a duplicate "final=true"
    # transcript to the client. Other observer features (bot speaking,
    # transcripts, metrics, …) stay on.
    observer_params = RTVIObserverParams(user_transcription_enabled=False)
    observers: list[BaseObserver] = [RTVIObserver(rtvi, params=observer_params)]
    task = PipelineTask(pipeline, observers=observers)

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception:
        logger.exception("voice: pipeline run failed | session={}", session.id)


def _parse_narrate_tools(data: object) -> bool | None:
    """Extract the ``narrateTools`` flag from an end-turn payload, if any."""
    if not isinstance(data, dict):
        return None
    val = data.get("narrateTools")
    if isinstance(val, bool):
        return val
    return None


def _parse_tts_enabled(data: object) -> bool | None:
    """Extract the ``enabled`` flag from a set-tts payload, if any."""
    if not isinstance(data, dict):
        return None
    val = data.get("enabled")
    if isinstance(val, bool):
        return val
    return None


def _parse_model(data: object) -> ModelChoice | None:
    """Pull a ``{providerID, modelID}`` pair out of an RTVI client-message
    payload. Defensive — the field is optional and the wire is JSON, so
    anything unexpected just returns ``None``."""
    if not isinstance(data, dict):
        return None
    model = data.get("model")
    if not isinstance(model, dict):
        return None
    provider_id = model.get("providerID")
    model_id = model.get("modelID")
    if isinstance(provider_id, str) and isinstance(model_id, str):
        return ModelChoice(provider_id=provider_id, model_id=model_id)
    return None


async def shutdown() -> None:
    """No-op for compatibility with the old WebRTC handler.

    The WebSocket transport has no process-wide handler to close; each
    connection owns its own pipeline lifecycle and tears down on disconnect.
    """


def _select_stt() -> STTService:
    """Pick an STT provider based on which API key is set.

    ElevenLabs realtime wins over Deepgram when both are present.
    Override with ``FRIDAY_STT_PROVIDER=elevenlabs`` or ``=deepgram``.

    ElevenLabs is configured for VAD-strategy commits at 500ms silence:
    early-and-often segmentation keeps ElevenLabs' audio buffer small
    (lower ASR latency), prevents the 90s force-commit from ever firing,
    and lets the ``TurnAccumulator`` reason about a steady stream of
    fragments rather than one giant chunk. We use the force-commit shim
    so tap-to-send still flushes trailing audio in VAD mode (pipecat's
    stock service only sends manual commits in MANUAL mode).
    """
    forced = os.environ.get("FRIDAY_STT_PROVIDER", "").lower()
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")
    deepgram_key = os.environ.get("DEEPGRAM_API_KEY")

    use_elevenlabs = forced == "elevenlabs" or (forced == "" and elevenlabs_key is not None)
    if use_elevenlabs:
        if not elevenlabs_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        return ElevenLabsRealtimeSTTServiceForceCommit(
            api_key=elevenlabs_key,
            commit_strategy=CommitStrategy.VAD,
            settings=ElevenLabsRealtimeSTTSettings(vad_silence_threshold_secs=0.5),
        )
    if not deepgram_key:
        raise RuntimeError("set ELEVENLABS_API_KEY or DEEPGRAM_API_KEY")
    return DeepgramSTTService(api_key=deepgram_key)


def _select_tts() -> TTSService:
    """Pick a TTS provider based on which API key is set.

    ElevenLabs wins over Cartesia when both are present, since it's the
    higher-quality default. Override with ``FRIDAY_TTS_PROVIDER=cartesia``
    or ``=elevenlabs`` to force one. Voice ID comes from
    ``FRIDAY_TTS_VOICE_ID`` (provider-specific).
    """
    forced = os.environ.get("FRIDAY_TTS_PROVIDER", "").lower()
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")
    cartesia_key = os.environ.get("CARTESIA_API_KEY")
    voice_id = os.environ.get("FRIDAY_TTS_VOICE_ID")

    use_elevenlabs = forced == "elevenlabs" or (forced == "" and elevenlabs_key is not None)
    if use_elevenlabs:
        if not elevenlabs_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        # Rachel — ElevenLabs' classic stock voice. ElevenLabs requires
        # a real voice_id; passing None makes the websocket reject with
        # 1008 policy violation and the pipeline dies before audio.
        return ElevenLabsTTSService(
            api_key=elevenlabs_key,
            voice_id=voice_id or "21m00Tcm4TlvDq8ikWAM",
        )
    if not cartesia_key:
        raise RuntimeError("set ELEVENLABS_API_KEY or CARTESIA_API_KEY")
    return CartesiaTTSService(
        api_key=cartesia_key,
        voice_id=voice_id or "79a125e8-cd45-4c13-8a67-188112f4dd22",
    )
