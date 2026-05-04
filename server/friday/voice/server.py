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
        → STT (ElevenLabs realtime, or Deepgram)   # owns its own VAD
        → OpencodeProcessor                         # replaces the LLM slot
        → TTS (ElevenLabs, or Cartesia)
        → transport.output()
        → RTVIProcessor                             # voice-UI state surface

Why no Silero VAD anymore: ElevenLabs realtime STT and Deepgram both ship
with their own VAD that knows their commit semantics. With pipecat's Silero
in front, the local VAD's ``stop_secs=0.2`` cut off audio before
ElevenLabs' ``vad_silence_threshold_secs=1.5`` saw enough trailing silence
to commit a transcript — second turn never finalized. Letting STT do its
own VAD is the simpler, working path. If we ever swap to a STT without
built-in VAD, re-add Silero here.

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
from pipecat.frames.frames import VADUserStoppedSpeakingFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.models import ClientMessage
from pipecat.processors.frameworks.rtvi.observer import RTVIObserver
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

from friday.core.session_manager import SessionManager
from friday.core.opencode_session import SYSTEM_PROMPT_VOICE
from friday.voice.pipecat_adapter import OpencodeProcessor

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
        opencode_session = manager.attach(session_id)
        logger.info("voice: attached to opencode session | id={}", opencode_session.id)
    else:
        opencode_session = await manager.create(system_prompt=SYSTEM_PROMPT_VOICE)
        logger.info("voice: created new opencode session | id={}", opencode_session.id)

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
    opencode = OpencodeProcessor(opencode_session)
    rtvi = RTVIProcessor(transport=transport)

    # Echo client-ready back as bot-ready. Pipecat's RTVIProcessor only
    # sends bot-ready when the app explicitly signals it — the convention
    # in WebRTC apps is to wait for a separate bot worker to load models /
    # connect to the LLM. friday's pipeline is ready the moment the WS
    # accepts the connection (STT/TTS WebSockets are dialled inside the
    # PipelineRunner before ``StartFrame`` propagates), so we ack as soon
    # as the client says hello. Without this, voice-ui-kit's ClientStatus
    # pill reads "Agent connecting" forever even though everything works.
    async def _on_client_ready(processor: RTVIProcessor) -> None:
        await processor.set_bot_ready()

    rtvi.event_handler("on_client_ready")(_on_client_ready)

    # Tap-to-end-turn: client sends {type: "end-turn"} when the user is
    # done speaking; we forge a VADUserStoppedSpeakingFrame upstream so
    # the STT processor sends {commit: True} to ElevenLabs and locks in
    # the in-progress transcript. Pipecat's ElevenLabs realtime STT is
    # in MANUAL commit mode by default, but we have no upstream VAD —
    # this handler is the only thing that triggers commits.
    #
    # If we ever add hands-free conversational mode, swap this for a
    # real VAD (Silero) tuned to long pauses, or use ElevenLabs's own
    # VAD via ``commit_strategy=CommitStrategy.VAD`` with a generous
    # ``vad_silence_threshold_secs``.
    async def _on_client_message(processor: RTVIProcessor, msg: ClientMessage) -> None:
        if msg.type == "end-turn":
            logger.info("voice: end-turn received | session={}", opencode_session.id)
            await processor.push_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

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
            opencode,
            tts,
            rtvi,
            transport.output(),
        ]
    )

    observers: list[BaseObserver] = [RTVIObserver(rtvi)]
    task = PipelineTask(pipeline, observers=observers)

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception:
        logger.exception("voice: pipeline run failed | session={}", opencode_session.id)


async def shutdown() -> None:
    """No-op for compatibility with the old WebRTC handler.

    The WebSocket transport has no process-wide handler to close; each
    connection owns its own pipeline lifecycle and tears down on disconnect.
    """


def _select_stt() -> STTService:
    """Pick an STT provider based on which API key is set.

    ElevenLabs realtime wins over Deepgram when both are present.
    Override with ``FRIDAY_STT_PROVIDER=elevenlabs`` or ``=deepgram``.
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
