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
        → STT (ElevenLabs realtime manual/Pipecat-VAD mode, or Deepgram)
        → UserTranscriptMirror                 # UI transcript messages only
        → LLMUserAggregator                    # Pipecat owns turn boundaries
        → ProviderSessionProcessor             # replaces the LLM slot
        → TTS (ElevenLabs, or Cartesia)
        → transport.output()
        → RTVIProcessor                             # voice-UI state surface

Pipecat owns turn-taking. STT transcript finalization only means "these
words were recognized"; ``LLMUserAggregator`` waits for real VAD stop plus a
coding-assistant pause window before Friday sends a completed turn to the
provider.

Notes:

- App data (sessions, transcripts, agent state) flows through REST/SSE in
  ``friday.api.sessions`` — not via RTVI custom messages. RTVI is voice-UI
  state only.
- ``?session_id=…`` for existing sessions, or ``?harness=…&directory=…`` to
  create a new session. Missing params closes the socket with 1003.
- Auth (Step 6) will accept ``?t=…`` here once it lands; until then the
  endpoint is open and expects to be reachable on localhost.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from typing import Any

from fastapi import APIRouter, WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import InterruptionTaskFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.models import ClientMessage
from pipecat.processors.frameworks.rtvi.observer import RTVIObserver, RTVIObserverParams
from pipecat.processors.frameworks.rtvi.processor import RTVIProcessor
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from friday.core.provider import ModelChoice, Provider
from friday.core.session_registry import ProviderRegistry
from friday.voice.pipecat_adapter import ProviderSessionProcessor, UserTranscriptMirror

router = APIRouter(tags=["voice"])


# We can't use the HTTP-flavored ``get_provider`` Depends from
# ``friday.api.sessions`` here — FastAPI's WebSocket scope doesn't have a
# ``Request`` to inject. Read straight off ``app.state`` instead.
async def _resolve_provider_for_session(websocket: WebSocket, session_id: str) -> Provider | None:
    """Look up which provider owns the session; close the WS with an error if missing."""
    registry: ProviderRegistry | None = getattr(websocket.app.state, "registry", None)
    if registry is None:
        await websocket.close(code=1011, reason="provider not ready")
        return None
    provider = await registry.resolve_for_session(session_id)
    if provider is None:
        await websocket.close(code=1008, reason=f"session not found: {session_id}")
        return None
    return provider


# Sample rates match the @pipecat-ai/websocket-transport client defaults
# (recorderSampleRate=16000 for mic input, playerSampleRate=24000 for TTS
# output). Keeping these aligned avoids resampling on either side.
_AUDIO_IN_SAMPLE_RATE = 16_000
_AUDIO_OUT_SAMPLE_RATE = 24_000
_USER_SPEECH_TIMEOUT_SECS = 1.5


@router.websocket("/api/voice")
async def voice(  # noqa: PLR0911, PLR0915
    websocket: WebSocket,
    session_id: str | None = None,
    harness: str | None = None,
    directory: str | None = None,
    title: str | None = None,
) -> None:
    """Bidirectional voice stream.

    Two modes:
    - Existing session: ``?session_id=…``
    - New session: ``?harness=<id>&directory=<path>`` (+ optional ``&title=…``)

    For new sessions the server creates the provider session here; once the
    SDK assigns a UUID (on the first turn) a ``session-created`` RTVI server
    message is pushed to the client with the real ``session_id``.

    Closes 1003 if neither form of params is provided, 1008 if the harness or
    session is unknown, 1011 if the provider registry is not ready.
    """
    registry: ProviderRegistry | None = getattr(websocket.app.state, "registry", None)
    is_new_session = False

    if session_id:
        # Existing session — resolve via registry.
        await websocket.accept()
        provider = await _resolve_provider_for_session(websocket, session_id)
        if provider is None:
            return
        session = provider.attach(session_id)
        logger.info(
            "voice: attached to existing session | id={} provider={}",
            session.id,
            provider.provider_id,
        )
    elif harness and directory:
        # New session — pick provider by harness and create it lazily.
        if registry is None:
            await websocket.close(code=1011, reason="provider not ready")
            return
        path = pathlib.Path(directory)
        if not path.is_absolute():
            await websocket.close(code=1008, reason="directory must be absolute")
            return
        if not await asyncio.to_thread(path.is_dir):
            await websocket.close(code=1008, reason=f"directory does not exist: {directory}")
            return
        provider = registry.get(harness)
        if provider is None:
            await websocket.close(code=1008, reason=f"unknown harness: {harness!r}")
            return
        await websocket.accept()
        session = await provider.create_session(title=title, directory=directory)
        is_new_session = True
        # Providers with an immediate ID (opencode) can be registered now.
        if session.id:
            registry.register_session(session.id, provider.provider_id)
        logger.info(
            "voice: new session created | harness={} directory={} id={}",
            harness,
            directory,
            session.id or "<pending>",
        )
    else:
        await websocket.close(
            code=1003,
            reason="session_id required for existing sessions; harness+directory for new",
        )
        return

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

    services = await _select_voice_services(websocket, session.id)
    if services is None:
        return
    stt, tts = services

    transcript_mirror = UserTranscriptMirror()
    user_aggregator = _make_user_aggregator()
    agent = ProviderSessionProcessor(session)
    rtvi = RTVIProcessor(transport=transport)
    _wire_turn_dispatcher(user_aggregator, transcript_mirror, agent)

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
        if is_new_session:
            if session.id:
                # Provider already has a real ID (e.g. opencode) — tell client now.
                await processor.send_server_message(
                    {"type": "session-created", "session_id": session.id}
                )
            elif hasattr(session, "_on_sdk_id_assigned"):
                # Provider assigns the ID on the first turn (e.g. claude-code).
                # Set a callback so we notify the client as soon as it arrives.
                async def _on_sdk_id(sdk_id: str) -> None:
                    if hasattr(provider, "register_session_by_sdk_id"):
                        provider.register_session_by_sdk_id(session, sdk_id)  # type: ignore[arg-type]
                    if registry is not None:
                        registry.register_session(sdk_id, provider.provider_id)
                    await processor.send_server_message(
                        {"type": "session-created", "session_id": sdk_id}
                    )

                session._on_sdk_id_assigned = _on_sdk_id  # type: ignore[attr-defined]

    rtvi.event_handler("on_client_ready")(_on_client_ready)

    # Client messages carry sticky Friday settings plus manual controls. Turn
    # finalization itself is still owned by Pipecat's VAD/user-turn aggregator;
    # end-turn deliberately does not forge speech-stop frames.
    async def _on_client_message(processor: RTVIProcessor, msg: ClientMessage) -> None:
        if msg.type == "end-turn":
            # Backward-compatible metadata handling for older clients. New
            # clients assert these as sticky settings before speech starts.
            model = _parse_model(msg.data)
            if model is not None:
                agent.current_model = model
            narrate = _parse_narrate_tools(msg.data)
            if narrate is not None:
                agent.narrate_tools = narrate
            logger.info(
                "voice: end-turn metadata received | session={} model={} narrate_tools={}",
                session.id,
                agent.current_model,
                agent.narrate_tools,
            )
        elif msg.type == "set-model":
            model = _parse_model(msg.data)
            if model is not None:
                agent.current_model = model
                logger.info(
                    "voice: set-model | session={} model={}",
                    session.id,
                    agent.current_model,
                )
        elif msg.type == "set-narrate-tools":
            narrate = _parse_narrate_tools(msg.data)
            if narrate is not None:
                agent.narrate_tools = narrate
                logger.info(
                    "voice: set-narrate-tools | session={} enabled={}",
                    session.id,
                    agent.narrate_tools,
                )
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
            transcript_mirror,
            user_aggregator,
            agent,
            tts,
            rtvi,
            transport.output(),
        ]
    )

    # Disable the observer's built-in user-transcription auto-emit. The
    # mirror owns user-transcript messaging now via two custom RTVI
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
    """Extract the tool narration flag from a client-message payload, if any."""
    if not isinstance(data, dict):
        return None
    val = data.get("narrateTools", data.get("enabled"))
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
    if model is None and "providerID" in data and "modelID" in data:
        model = data
    if not isinstance(model, dict):
        return None
    provider_id = model.get("providerID")
    model_id = model.get("modelID")
    if isinstance(provider_id, str) and isinstance(model_id, str):
        return ModelChoice(provider_id=provider_id, model_id=model_id)
    return None


async def _select_voice_services(
    websocket: WebSocket, session_id: str
) -> tuple[STTService, TTSService] | None:
    try:
        return _select_stt(), _select_tts()
    except RuntimeError as exc:
        logger.error("voice: provider configuration failed | session={} err={}", session_id, exc)
        await websocket.close(code=1011, reason=str(exc))
        return None


def _make_user_aggregator() -> LLMUserAggregator:
    """Build the Pipecat user-turn owner for Friday voice turns."""
    return LLMUserAggregator(
        LLMContext(),
        params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                start=[
                    VADUserTurnStartStrategy(enable_interruptions=False),
                    TranscriptionUserTurnStartStrategy(enable_interruptions=False),
                ],
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=_USER_SPEECH_TIMEOUT_SECS
                    )
                ],
            ),
        ),
    )


def _wire_turn_dispatcher(
    user_aggregator: LLMUserAggregator,
    transcript_mirror: UserTranscriptMirror,
    agent: ProviderSessionProcessor,
) -> None:
    """Route completed Pipecat user turns into Friday provider sessions."""

    async def on_user_turn_stopped(
        _aggregator: LLMUserAggregator,
        _strategy: object,
        message: Any,
    ) -> None:
        text = getattr(message, "content", "").strip()
        if not text:
            transcript_mirror.reset()
            return
        await transcript_mirror.emit_final(text)
        await agent.send_user_turn(text)

    user_aggregator.add_event_handler("on_user_turn_stopped", on_user_turn_stopped)


async def shutdown() -> None:
    """No-op for compatibility with the old WebRTC handler.

    The WebSocket transport has no process-wide handler to close; each
    connection owns its own pipeline lifecycle and tears down on disconnect.
    """


def _select_stt() -> STTService:
    """Pick an STT provider based on which API key is set.

    ElevenLabs realtime wins over Deepgram when both are present.
    Override with ``FRIDAY_STT_PROVIDER=elevenlabs`` or ``=deepgram``.

    ElevenLabs uses its default manual commit strategy. In Pipecat terms this
    means local VAD owns speech stop; the STT service commits only when Pipecat
    observes a real ``VADUserStoppedSpeakingFrame``.
    """
    forced = os.environ.get("FRIDAY_STT_PROVIDER", "").lower()
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")
    deepgram_key = os.environ.get("DEEPGRAM_API_KEY")

    use_elevenlabs = forced == "elevenlabs" or (forced == "" and elevenlabs_key is not None)
    if use_elevenlabs:
        if not elevenlabs_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        return ElevenLabsRealtimeSTTService(api_key=elevenlabs_key)
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
