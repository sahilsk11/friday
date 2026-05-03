"""WebRTC signaling + per-connection pipeline assembly.

Mounts ``POST /voice/api/offer`` and ``PATCH /voice/api/offer`` onto the same
FastAPI app as the REST/SSE session router. The browser exchanges SDP with
those endpoints; ``SmallWebRTCRequestHandler`` keeps track of peer
connections and invokes our callback when a new one is established.

Pipeline shape per connection::

    transport.input()
        → STT (Deepgram)
        → OpencodeProcessor       # replaces the LLM slot
        → TTS (Cartesia)
        → transport.output()
        → RTVIObserver            # surfaces voice-UI state to voice-ui-kit

Notes:

- App data (sessions, transcripts, agent state) flows through REST/SSE in
  ``friday.api.sessions`` — **not** via RTVI custom messages. RTVI here is
  read-only voice-UI state.
- Auth lives in Step 6 (config). For now the offer endpoint is open and
  expects to be reachable on localhost.
- We accept ``request.request_data["session_id"]`` to attach to an existing
  opencode session; if absent, we create a new one. The browser is expected
  to call ``GET /sessions`` first and pass the chosen id here.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frameworks.rtvi.observer import RTVIObserver
from pipecat.processors.frameworks.rtvi.processor import RTVIProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from friday.core.session_manager import SessionManager
from friday.voice.pipecat_adapter import OpencodeProcessor

router = APIRouter(prefix="/voice", tags=["voice"])

# Shared across the process. Lifespan hooks tear it down via ``close()``.
_handler = SmallWebRTCRequestHandler()


def _require_manager(request: Request) -> SessionManager:
    manager: SessionManager | None = getattr(request.app.state, "manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="session manager not ready")
    return manager


@router.post("/api/offer")
async def offer(
    request: Request,
    body: SmallWebRTCRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str] | None:
    manager = _require_manager(request)

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        background_tasks.add_task(_run_pipeline, connection, manager, body.request_data or {})

    return await _handler.handle_web_request(request=body, webrtc_connection_callback=on_connection)


@router.patch("/api/offer")
async def offer_patch(body: SmallWebRTCPatchRequest) -> dict[str, str]:
    await _handler.handle_patch_request(body)
    return {"status": "ok"}


async def shutdown() -> None:
    """Close all open WebRTC connections. Called from the FastAPI lifespan."""
    await _handler.close()


async def _run_pipeline(
    connection: SmallWebRTCConnection,
    manager: SessionManager,
    request_data: dict[str, Any],
) -> None:
    """Build and run the per-connection pipecat pipeline.

    Spawned as a FastAPI background task. Returns when the WebRTC connection
    closes; pipecat handles cleanup of its own tasks via ``TaskManager``.
    """
    session_id = request_data.get("session_id")
    if isinstance(session_id, str) and session_id:
        opencode_session = manager.attach(session_id)
    else:
        opencode_session = await manager.create()
        logger.info("voice: created new opencode session | id={}", opencode_session.id)

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())
    stt = _select_stt()
    tts = _select_tts()
    opencode = OpencodeProcessor(opencode_session)
    rtvi = RTVIProcessor(transport=transport)

    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            opencode,
            tts,
            transport.output(),
            rtvi,
        ]
    )

    observers: list[BaseObserver] = [RTVIObserver(rtvi)]
    task = PipelineTask(pipeline, observers=observers)

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception:
        # Log and let the connection close — the runner has already torn the
        # pipeline down by the time we reach here.
        logger.exception("voice: pipeline run failed | session={}", opencode_session.id)


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

    use_elevenlabs = (
        forced == "elevenlabs"
        or (forced == "" and elevenlabs_key is not None)
    )
    if use_elevenlabs:
        if not elevenlabs_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        return ElevenLabsTTSService(api_key=elevenlabs_key, voice_id=voice_id)
    if not cartesia_key:
        raise RuntimeError("set ELEVENLABS_API_KEY or CARTESIA_API_KEY")
    return CartesiaTTSService(
        api_key=cartesia_key,
        voice_id=voice_id or "79a125e8-cd45-4c13-8a67-188112f4dd22",
    )
