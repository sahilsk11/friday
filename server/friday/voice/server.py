"""WebRTC signaling + per-connection pipeline assembly.

Mounts ``POST /api/offer`` and ``PATCH /api/offer`` onto the same FastAPI app
as the REST/SSE session router. The browser exchanges SDP with those
endpoints; ``SmallWebRTCRequestHandler`` keeps track of peer connections and
invokes our callback when a new one is established.

URL choice: we use ``/api/offer`` (no prefix) to match the default
``connectParams.webrtcUrl`` that voice-ui-kit ships with — see
[voice-ui-kit/examples/04-vite/src/main.tsx]. A frontend serving from a
different origin can override the URL on its side; we don't need to.

Body handling: pipecat's ``SmallWebRTCRequest`` / ``SmallWebRTCPatchRequest``
are plain ``@dataclass`` types — FastAPI's pydantic validator can't introspect
them and 422s on every request if we annotate route bodies with these types.
We accept raw JSON and construct the dataclasses manually.

Pipeline shape per connection::

    transport.input()
        → VAD (Silero)
        → STT (ElevenLabs realtime, or Deepgram)
        → OpencodeProcessor       # replaces the LLM slot
        → TTS (ElevenLabs, or Cartesia)
        → transport.output()
        → RTVIProcessor           # surfaces voice-UI state to voice-ui-kit

Notes:

- App data (sessions, transcripts, agent state) flows through REST/SSE in
  ``friday.api.sessions`` — **not** via RTVI custom messages. RTVI is read-only
  voice-UI state.
- Auth lives in Step 6 (config). For now the offer endpoint is open and
  expects to be reachable on localhost.
- ``request.request_data["session_id"]`` attaches to an existing opencode
  session; if absent, we create a new one. The browser is expected to call
  ``GET /sessions`` first and pass the chosen id here.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
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
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from friday.api.sessions import get_manager
from friday.core.session_manager import SessionManager
from friday.voice.pipecat_adapter import OpencodeProcessor

router = APIRouter(tags=["voice"])

# Shared across the process. Lifespan tears it down via ``shutdown()``.
_handler = SmallWebRTCRequestHandler()

ManagerDep = Annotated[SessionManager, Depends(get_manager)]


@router.post("/api/offer")
async def offer(
    request: Request, manager: ManagerDep, background_tasks: BackgroundTasks
) -> dict[str, str] | None:
    body = await request.json()
    try:
        webrtc_request = SmallWebRTCRequest(
            sdp=body["sdp"],
            type=body["type"],
            pc_id=body.get("pc_id"),
            restart_pc=body.get("restart_pc"),
            request_data=body.get("request_data"),
        )
    except KeyError as err:
        raise HTTPException(status_code=400, detail=f"missing field: {err.args[0]}") from err

    request_data: dict[str, Any] = webrtc_request.request_data or {}

    async def on_connection(connection: SmallWebRTCConnection) -> None:
        background_tasks.add_task(_run_pipeline, connection, manager, request_data)

    return await _handler.handle_web_request(
        request=webrtc_request, webrtc_connection_callback=on_connection
    )


@router.patch("/api/offer")
async def offer_patch(request: Request) -> dict[str, str]:
    body = await request.json()
    try:
        candidates_raw = body.get("candidates") or []
        candidates = [
            IceCandidate(
                candidate=c["candidate"],
                sdp_mid=c.get("sdpMid") or c.get("sdp_mid") or "",
                sdp_mline_index=c.get("sdpMLineIndex") or c.get("sdp_mline_index") or 0,
            )
            for c in candidates_raw
        ]
        patch = SmallWebRTCPatchRequest(pc_id=body["pc_id"], candidates=candidates)
    except KeyError as err:
        raise HTTPException(status_code=400, detail=f"missing field: {err.args[0]}") from err

    await _handler.handle_patch_request(patch)
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
