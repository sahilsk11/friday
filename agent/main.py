from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, TurnHandlingOptions, cli
from livekit.agents.voice import room_io
from livekit.plugins import elevenlabs
from pydantic import ValidationError

from agent.config import AgentSettings, get_agent_settings
from agent.livekit_message_mapper import agent_response_from_voice_message
from agent.narrator_client import HttpNarratorBackendClient
from agent.protocol import (
    AGENT_RESPONSE_TOPIC,
    TURN_CONTROL_RPC_METHODS,
    AgentResponse,
    TurnControlMessage,
    TurnControlResult,
    TurnControlType,
)
from friday.application.voice import (
    NarratorBackendClient,
    NarratorEvent,
    NarratorEventRelay,
    VoiceAgentMessage,
    VoiceInteractionService,
    VoicePlaybackState,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("friday.livekit.agent")

_LIVEKIT_AGENT_NAME = get_agent_settings().livekit_agent_name
server = AgentServer()
VOICE_TRANSCRIPTION_LOST_MESSAGE = (
    "Voice agent lost the speech session before transcription finished. "
    "Reconnect voice and try again."
)
STT_DISCONNECTED_MESSAGE = (
    "Speech-to-text disconnected before transcription finished. Reconnect voice and try again."
)


def current_settings() -> AgentSettings:
    return get_agent_settings()


def create_session() -> AgentSession[None]:
    settings = current_settings()
    logger.info(
        "Configuring ElevenLabs STT model=%s commit_strategy=vad silence_threshold=%ss "
        "min_silence=%sms min_speech=%sms",
        settings.elevenlabs_stt_model,
        settings.elevenlabs_vad_silence_threshold_secs,
        settings.elevenlabs_min_silence_duration_ms,
        settings.elevenlabs_min_speech_duration_ms,
    )
    return AgentSession(
        stt=elevenlabs.STT(
            api_key=settings.api_key,
            model_id=settings.elevenlabs_stt_model,
            language_code=settings.elevenlabs_language_code,
            server_vad={
                "vad_silence_threshold_secs": settings.elevenlabs_vad_silence_threshold_secs,
                "vad_threshold": settings.elevenlabs_vad_threshold,
                "min_speech_duration_ms": settings.elevenlabs_min_speech_duration_ms,
                "min_silence_duration_ms": settings.elevenlabs_min_silence_duration_ms,
            },
        ),
        tts=elevenlabs.TTS(
            api_key=settings.api_key,
            model=settings.elevenlabs_tts_model,
            voice_id=settings.elevenlabs_tts_voice_id,
        ),
        turn_handling=TurnHandlingOptions(turn_detection="manual"),
        user_away_timeout=None,
    )


async def _send_agent_response(room: rtc.Room, response: AgentResponse) -> None:
    payload = response.model_dump_json(exclude_none=True).encode("utf-8")
    await room.local_participant.publish_data(
        payload,
        topic=AGENT_RESPONSE_TOPIC,
    )


async def _send_voice_message(room: rtc.Room, message: VoiceAgentMessage) -> None:
    await _send_agent_response(room, agent_response_from_voice_message(message))


async def _submit_narrator_turn(
    *,
    narrator_client: NarratorBackendClient,
    playback_state: VoicePlaybackState,
    relay: NarratorEventRelay,
    voice_interactions: VoiceInteractionService,
    room: rtc.Room,
    session: AgentSession[None],
    session_id: str,
    source: str,
    text: str,
) -> None:
    await _send_agent_response(
        room,
        AgentResponse(type="state", state="thinking"),
    )
    try:
        events = await narrator_client.submit_turn(
            session_id=session_id,
            source=source,
            text=text,
        )
        for event in await relay.unseen(events):
            await _handle_narrator_event(
                room=room,
                session=session,
                playback_state=playback_state,
                voice_interactions=voice_interactions,
                event=event,
            )
        logger.info("Narrator turn submitted successfully source=%s", source)
    except Exception as err:
        logger.error("Narrator submit_turn failed source=%s: %s", source, err, exc_info=True)
        await _send_agent_response(
            room,
            AgentResponse(type="error", message=str(err)),
        )
        await _send_agent_response(
            room,
            AgentResponse(type="state", state="idle"),
        )


async def handle_turn_control(
    *,
    session: AgentSession[None],
    narrator_client: NarratorBackendClient,
    playback_state: VoicePlaybackState,
    relay: NarratorEventRelay,
    voice_interactions: VoiceInteractionService,
    room: rtc.Room,
    room_name: str,
    session_id: str,
    participant_identity: str,
    message: TurnControlMessage,
) -> TurnControlResult:
    logger.info("Received %s from %s", message.type, participant_identity)

    if message.type == "submit_text":
        text = (message.text or "").strip()
        if not text:
            await _send_agent_response(
                room,
                AgentResponse(type="error", message="Text turn was empty."),
            )
            await _send_agent_response(
                room,
                AgentResponse(type="state", state="idle"),
            )
            return TurnControlResult(
                ok=False,
                type=message.type,
                message="Text turn was empty.",
                state="idle",
            )
        playback_state.user_turn_open = False
        with contextlib.suppress(Exception):
            session.input.set_audio_enabled(False)
        if playback_state.session_error_message is None:
            with contextlib.suppress(Exception):
                await session.interrupt(force=True)
        logger.info(
            "Submitting text turn through agent room=%s participant=%s text=%r",
            room_name,
            participant_identity,
            text,
        )
        asyncio.create_task(
            _submit_narrator_turn(
                narrator_client=narrator_client,
                playback_state=playback_state,
                relay=relay,
                voice_interactions=voice_interactions,
                room=room,
                session=session,
                session_id=session_id,
                source="text",
                text=text,
            )
        )
        return TurnControlResult(ok=True, type=message.type, state="thinking")

    if playback_state.session_error_message is not None:
        await _send_agent_response(
            room,
            AgentResponse(
                type="error",
                message=playback_state.session_error_message,
            ),
        )
        await _send_agent_response(
            room,
            AgentResponse(type="state", state="idle"),
        )
        return TurnControlResult(
            ok=False,
            type=message.type,
            message=playback_state.session_error_message,
            state="idle",
        )

    if message.type == "set_speaker":
        playback_state.speaker_enabled = message.speaker_enabled is not False
        if not playback_state.speaker_enabled:
            await session.interrupt(force=True)
        return TurnControlResult(ok=True, type=message.type)

    if message.type == "start_turn":
        try:
            playback_state.user_turn_open = True
            await session.interrupt(force=True)
            session.clear_user_turn()
            if participant_identity != "unknown" and session.room_io:
                session.room_io.set_participant(participant_identity)
                logger.info(
                    "Voice input bound to participant=%s for room=%s",
                    participant_identity,
                    room_name,
                )
            session.input.set_audio_enabled(True)
            await _send_agent_response(
                room,
                AgentResponse(type="state", state="listening"),
            )
            return TurnControlResult(ok=True, type=message.type, state="listening")
        except Exception as err:
            playback_state.user_turn_open = False
            logger.error("Unable to start voice turn: %s", err, exc_info=True)
            await _send_agent_response(
                room,
                AgentResponse(
                    type="error",
                    message="Voice agent is not ready. Reconnect voice and try again.",
                ),
            )
            await _send_agent_response(
                room,
                AgentResponse(type="state", state="idle"),
            )
            return TurnControlResult(
                ok=False,
                type=message.type,
                message="Voice agent is not ready. Reconnect voice and try again.",
                state="idle",
            )

    if message.type == "end_turn":
        settings = current_settings()
        logger.info("end_turn received; disabling audio and committing user turn")
        playback_state.user_turn_open = False
        try:
            session.input.set_audio_enabled(False)
            await _send_agent_response(
                room,
                AgentResponse(type="state", state="transcribing"),
            )
            transcript_future = session.commit_user_turn(
                transcript_timeout=settings.friday_commit_transcript_timeout_secs,
                stt_flush_duration=settings.friday_commit_stt_flush_duration_secs,
                skip_reply=True,
            )
            transcript = (await transcript_future).strip()
        except Exception as err:
            logger.error("Unable to commit voice turn: %s", err, exc_info=True)
            await _send_agent_response(
                room,
                AgentResponse(
                    type="error",
                    message=VOICE_TRANSCRIPTION_LOST_MESSAGE,
                ),
            )
            await _send_agent_response(
                room,
                AgentResponse(type="state", state="idle"),
            )
            return TurnControlResult(
                ok=False,
                type=message.type,
                message=VOICE_TRANSCRIPTION_LOST_MESSAGE,
                state="idle",
            )
        logger.info("Transcript result: %r (empty=%s)", transcript, not transcript)
        if transcript:
            logger.info(
                "Committed transcript room=%s participant=%s transcript=%r",
                room_name,
                participant_identity,
                transcript,
            )
            await _send_agent_response(
                room,
                AgentResponse(type="state", state="thinking"),
            )
            await _send_agent_response(
                room,
                AgentResponse(type="transcript", text=transcript),
            )
            asyncio.create_task(
                _submit_narrator_turn(
                    narrator_client=narrator_client,
                    playback_state=playback_state,
                    relay=relay,
                    voice_interactions=voice_interactions,
                    room=room,
                    session=session,
                    session_id=session_id,
                    source="voice",
                    text=transcript,
                )
            )
            return TurnControlResult(
                ok=True,
                type=message.type,
                state="thinking",
                transcript=transcript,
            )
        else:
            logger.warning("Transcript was empty; skipping provider send_turn")
            await _send_agent_response(
                room,
                AgentResponse(
                    type="error",
                    message="No speech was transcribed for that turn.",
                ),
            )
            await _send_agent_response(
                room,
                AgentResponse(type="state", state="idle"),
            )
            return TurnControlResult(
                ok=False,
                type=message.type,
                message="No speech was transcribed for that turn.",
                state="idle",
            )

    playback_state.user_turn_open = False
    await _send_agent_response(
        room,
        AgentResponse(type="state", state="idle"),
    )
    await session.interrupt(force=True)
    events = await narrator_client.cancel(session_id=session_id)
    for event in await relay.unseen(events):
        await _handle_narrator_event(
            room=room,
            session=session,
            playback_state=playback_state,
            voice_interactions=voice_interactions,
            event=event,
        )
    session.input.set_audio_enabled(False)
    session.clear_user_turn()
    return TurnControlResult(ok=True, type=message.type, state="idle")


def _turn_control_message_from_rpc(
    command_type: TurnControlType,
    data: rtc.RpcInvocationData,
) -> TurnControlMessage:
    try:
        raw_payload = json.loads(data.payload) if data.payload else {}
    except json.JSONDecodeError as err:
        raise rtc.RpcError(
            rtc.RpcError.ErrorCode.APPLICATION_ERROR,
            "Invalid turn control payload.",
        ) from err

    if not isinstance(raw_payload, dict):
        raise rtc.RpcError(
            rtc.RpcError.ErrorCode.APPLICATION_ERROR,
            "Turn control payload must be a JSON object.",
        )

    payload = dict(raw_payload)
    payload_type = payload.setdefault("type", command_type)
    if payload_type != command_type:
        raise rtc.RpcError(
            rtc.RpcError.ErrorCode.APPLICATION_ERROR,
            "Turn control RPC method did not match payload type.",
        )

    try:
        return TurnControlMessage.model_validate(payload)
    except ValidationError as err:
        raise rtc.RpcError(
            rtc.RpcError.ErrorCode.APPLICATION_ERROR,
            "Invalid turn control payload.",
        ) from err


def register_turn_control_rpc_handlers(
    *,
    session: AgentSession[None],
    narrator_client: NarratorBackendClient,
    playback_state: VoicePlaybackState,
    relay: NarratorEventRelay,
    voice_interactions: VoiceInteractionService,
    room: rtc.Room,
    room_name: str,
    session_id: str,
) -> None:
    for command_type, method in TURN_CONTROL_RPC_METHODS.items():

        async def rpc_handler(
            data: rtc.RpcInvocationData,
            command_type: TurnControlType = command_type,
        ) -> str:
            message = _turn_control_message_from_rpc(command_type, data)
            async with playback_state.command_lock:
                result = await handle_turn_control(
                    session=session,
                    narrator_client=narrator_client,
                    playback_state=playback_state,
                    relay=relay,
                    voice_interactions=voice_interactions,
                    room=room,
                    room_name=room_name,
                    session_id=session_id,
                    participant_identity=data.caller_identity,
                    message=message,
                )
            return result.model_dump_json(exclude_none=True)

        room.local_participant.register_rpc_method(method, rpc_handler)


async def _initialize_narrator_cursor(
    *,
    narrator_client: NarratorBackendClient,
    session_id: str,
    relay: NarratorEventRelay,
) -> None:
    events = await narrator_client.list_events(session_id=session_id, after_id=0, limit=100)
    await relay.unseen(events)


async def _poll_narrator_events(
    *,
    narrator_client: NarratorBackendClient,
    room: rtc.Room,
    session: AgentSession[None],
    playback_state: VoicePlaybackState,
    session_id: str,
    relay: NarratorEventRelay,
    voice_interactions: VoiceInteractionService,
    interval_secs: float,
) -> None:
    while True:
        try:
            events = await narrator_client.list_events(
                session_id=session_id,
                after_id=relay.cursor,
                limit=50,
            )
            for event in await relay.unseen(events):
                await _handle_narrator_event(
                    room=room,
                    session=session,
                    playback_state=playback_state,
                    voice_interactions=voice_interactions,
                    event=event,
                )
        except Exception as err:
            logger.warning("Narrator event poll failed; retrying: %s", err)
        await asyncio.sleep(interval_secs)


async def _handle_narrator_event(
    *,
    room: rtc.Room,
    session: AgentSession[None],
    playback_state: VoicePlaybackState,
    voice_interactions: VoiceInteractionService,
    event: NarratorEvent,
) -> None:
    handling = voice_interactions.handle_narrator_event(event, playback_state)
    for message in handling.messages:
        await _send_voice_message(room, message)
    if handling.speech_text:
        session.say(handling.speech_text, allow_interruptions=True, add_to_chat_ctx=False)


def _parse_job_metadata(ctx: JobContext) -> tuple[str | None, str | None, str | None]:
    """Extract session routing data from the job's dispatch metadata."""
    metadata = getattr(ctx.job, "metadata", None) or ""
    if not metadata:
        return None, None, None
    try:
        data = json.loads(metadata)
        return data.get("session_id"), data.get("harness"), data.get("model_id")
    except (json.JSONDecodeError, AttributeError):
        return None, None, None


def _session_id_from_room_name(room_name: str) -> str | None:
    prefix = "friday-"
    if not room_name.startswith(prefix):
        return None
    session_id = room_name[len(prefix) :].strip()
    if "--" in session_id:
        session_id = session_id.split("--", 1)[0]
    return session_id or None


def _voice_session_error_message(error: object | None) -> str:
    if error is None:
        return "Voice agent session closed. Reconnect voice and try again."
    error_type = getattr(error, "type", "")
    if error_type == "stt_error":
        return STT_DISCONNECTED_MESSAGE
    return "Voice agent hit an audio pipeline error. Reconnect voice and try again."


@server.rtc_session(agent_name=_LIVEKIT_AGENT_NAME)
async def friday_agent(ctx: JobContext) -> None:
    session_id, harness, model_id = _parse_job_metadata(ctx)
    session_id = session_id or _session_id_from_room_name(ctx.room.name)
    logger.info(
        "Room joined: name=%s session_id=%s harness=%s model_id=%s",
        ctx.room.name,
        session_id,
        harness,
        model_id,
    )

    if session_id is None:
        logger.error("No session id available for room %s", ctx.room.name)
        return

    settings = current_settings()
    narrator_client = HttpNarratorBackendClient(settings.friday_api_base_url)

    session = create_session()
    session.input.set_audio_enabled(False)
    relay = NarratorEventRelay()
    playback_state = VoicePlaybackState()
    voice_interactions = VoiceInteractionService()
    shutdown_event = asyncio.Event()

    async def publish_voice_session_failure(error: object | None) -> None:
        message = _voice_session_error_message(error)
        playback_state.session_error_message = message
        playback_state.user_turn_open = False
        await _send_agent_response(
            ctx.room,
            AgentResponse(type="error", message=message),
        )
        await _send_agent_response(
            ctx.room,
            AgentResponse(type="state", state="idle"),
        )
        shutdown_event.set()

    @session.on("close")
    def on_session_close(event: object) -> None:
        error = getattr(event, "error", None)
        if error is None:
            return
        logger.error("Voice session closed with error: %s", error)
        asyncio.create_task(publish_voice_session_failure(error))

    @session.on("error")
    def on_session_error(event: object) -> None:
        error = getattr(event, "error", None)
        logger.error("Voice session error: %s", error)

    await _initialize_narrator_cursor(
        narrator_client=narrator_client,
        session_id=session_id,
        relay=relay,
    )
    poll_task = asyncio.create_task(
        _poll_narrator_events(
            narrator_client=narrator_client,
            room=ctx.room,
            session=session,
            playback_state=playback_state,
            session_id=session_id,
            relay=relay,
            voice_interactions=voice_interactions,
            interval_secs=settings.friday_narrator_poll_interval_secs,
        ),
        name=f"friday-narrator-poll-{session_id}",
    )

    @ctx.room.on("disconnected")
    def on_disconnected(_: object = None) -> None:
        shutdown_event.set()

    async def on_shutdown(_: str = "") -> None:
        shutdown_event.set()

    ctx.add_shutdown_callback(on_shutdown)

    try:
        await session.start(
            room=ctx.room,
            agent=Agent(
                instructions="Transcribe the user's speech. Do not generate a spoken reply.",
            ),
            room_options=room_io.RoomOptions(
                text_input=False,
                audio_output=True,
            ),
        )
        register_turn_control_rpc_handlers(
            session=session,
            narrator_client=narrator_client,
            playback_state=playback_state,
            relay=relay,
            voice_interactions=voice_interactions,
            room=ctx.room,
            room_name=ctx.room.name,
            session_id=session_id,
        )
        await shutdown_event.wait()
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
        await narrator_client.aclose()


if __name__ == "__main__":
    cli.run_app(server)
