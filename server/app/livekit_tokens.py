import asyncio
import json
import time
from datetime import timedelta
from uuid import uuid4

from livekit import api

from server.app.config import Settings

_agent_dispatch_locks: dict[str, asyncio.Lock] = {}
_IN_FLIGHT_DISPATCH_MAX_AGE_SECONDS = 30
_LIVEKIT_AGENT_PARTICIPANT_KIND = api.ParticipantInfo.Kind.Value("AGENT")
_ACTIVE_JOB_STATUSES = {
    api.JobStatus.Value("JS_PENDING"),
    api.JobStatus.Value("JS_RUNNING"),
}


def create_room_token(
    *,
    settings: Settings,
    room_name: str,
    identity: str,
    name: str,
) -> str:
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(name)
        .with_ttl(timedelta(seconds=settings.friday_token_ttl_seconds))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_publish_data=True,
                can_subscribe=True,
                can_publish_sources=["microphone"],
            )
        )
        .to_jwt()
    )


async def create_friday_room(
    *,
    settings: Settings,
    room_name: str,
    session_id: str | None = None,
    harness: str | None = None,
    model_id: str | None = None,
) -> None:
    metadata: dict[str, str] = {}
    if session_id:
        metadata["session_id"] = session_id
    if harness:
        metadata["harness"] = harness
    if model_id:
        metadata["model_id"] = model_id
    lk = api.LiveKitAPI(
        settings.livekit_server_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    )

    try:
        metadata_json = json.dumps(metadata) if metadata else ""
        try:
            await lk.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=600,
                    metadata=metadata_json,
                )
            )
        except api.TwirpError as err:
            if err.code != api.TwirpErrorCode.ALREADY_EXISTS:
                raise
            await lk.room.update_room_metadata(
                api.UpdateRoomMetadataRequest(
                    room=room_name,
                    metadata=metadata_json,
                )
            )

        async with _agent_dispatch_lock(room_name):
            if await _has_friday_agent_participant(lk, room_name):
                return
            if await _has_recent_friday_agent_dispatch(
                lk,
                room_name,
                agent_name=settings.livekit_agent_name,
                max_age_seconds=_IN_FLIGHT_DISPATCH_MAX_AGE_SECONDS,
            ):
                return
            await _redispatch_friday_agent(
                lk,
                room_name=room_name,
                agent_name=settings.livekit_agent_name,
                metadata_json=metadata_json,
            )
    finally:
        await lk.aclose()  # type: ignore[no-untyped-call]


async def ensure_friday_agent_dispatch(
    *,
    settings: Settings,
    room_name: str,
    session_id: str | None = None,
    harness: str | None = None,
    model_id: str | None = None,
) -> None:
    metadata: dict[str, str] = {}
    if session_id:
        metadata["session_id"] = session_id
    if harness:
        metadata["harness"] = harness
    if model_id:
        metadata["model_id"] = model_id

    lk = api.LiveKitAPI(
        settings.livekit_server_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    )
    try:
        metadata_json = json.dumps(metadata) if metadata else ""
        async with _agent_dispatch_lock(room_name):
            if await _has_friday_agent_participant(lk, room_name):
                return
            if await _has_recent_friday_agent_dispatch(
                lk,
                room_name,
                agent_name=settings.livekit_agent_name,
                max_age_seconds=_IN_FLIGHT_DISPATCH_MAX_AGE_SECONDS,
            ):
                return
            await _redispatch_friday_agent(
                lk,
                room_name=room_name,
                agent_name=settings.livekit_agent_name,
                metadata_json=metadata_json,
            )
    finally:
        await lk.aclose()  # type: ignore[no-untyped-call]


async def _redispatch_friday_agent(
    lk: api.LiveKitAPI,
    *,
    room_name: str,
    agent_name: str,
    metadata_json: str,
) -> None:
    await _delete_friday_agent_dispatches(lk, room_name, agent_name=agent_name)
    await _create_friday_agent_dispatch(
        lk,
        room_name=room_name,
        agent_name=agent_name,
        metadata_json=metadata_json,
    )


def _agent_dispatch_lock(room_name: str) -> asyncio.Lock:
    lock = _agent_dispatch_locks.get(room_name)
    if lock is None:
        lock = asyncio.Lock()
        _agent_dispatch_locks[room_name] = lock
    return lock


async def _create_friday_agent_dispatch(
    lk: api.LiveKitAPI,
    *,
    room_name: str,
    agent_name: str,
    metadata_json: str,
) -> None:
    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name=agent_name,
            room=room_name,
            metadata=metadata_json,
        )
    )


async def _has_friday_agent_participant(lk: api.LiveKitAPI, room_name: str) -> bool:
    try:
        participants = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
    except api.TwirpError as err:
        if err.code == api.TwirpErrorCode.NOT_FOUND:
            return False
        raise

    return any(
        participant.kind == _LIVEKIT_AGENT_PARTICIPANT_KIND
        for participant in participants.participants
    )


async def _list_agent_dispatches(lk: api.LiveKitAPI, room_name: str) -> list[api.AgentDispatch]:
    try:
        dispatches = await lk.agent_dispatch.list_dispatch(room_name)
    except api.TwirpError as err:
        if err.code == api.TwirpErrorCode.NOT_FOUND:
            return []
        raise
    return list(dispatches)


async def _list_friday_agent_dispatches(
    lk: api.LiveKitAPI,
    room_name: str,
    *,
    agent_name: str,
) -> list[api.AgentDispatch]:
    return [
        dispatch
        for dispatch in await _list_agent_dispatches(lk, room_name)
        if dispatch.agent_name == agent_name
    ]


async def _has_recent_friday_agent_dispatch(
    lk: api.LiveKitAPI,
    room_name: str,
    *,
    agent_name: str,
    max_age_seconds: int,
) -> bool:
    return any(
        _is_recent_active_agent_dispatch(dispatch, max_age_seconds=max_age_seconds)
        for dispatch in await _list_friday_agent_dispatches(
            lk,
            room_name,
            agent_name=agent_name,
        )
    )


def _is_recent_active_agent_dispatch(
    dispatch: api.AgentDispatch,
    *,
    max_age_seconds: int,
) -> bool:
    state = dispatch.state
    jobs = list(state.jobs)
    if not jobs:
        return _is_recent_timestamp(
            getattr(state, "created_at", 0),
            max_age_seconds=max_age_seconds,
        )

    for job in jobs:
        job_state = job.state
        if getattr(job_state, "status", None) not in _ACTIVE_JOB_STATUSES:
            continue
        newest_job_timestamp = max(
            getattr(job_state, "updated_at", 0),
            getattr(job_state, "started_at", 0),
            getattr(state, "created_at", 0),
        )
        if _is_recent_timestamp(
            newest_job_timestamp,
            max_age_seconds=max_age_seconds,
        ):
            return True
    return False


def _is_recent_timestamp(timestamp_ns: int, *, max_age_seconds: int) -> bool:
    return bool(timestamp_ns and time.time_ns() - timestamp_ns < max_age_seconds * 1_000_000_000)


async def _delete_friday_agent_dispatches(
    lk: api.LiveKitAPI,
    room_name: str,
    *,
    agent_name: str,
) -> None:
    for dispatch in await _list_friday_agent_dispatches(
        lk,
        room_name,
        agent_name=agent_name,
    ):
        if dispatch.id:
            try:
                await lk.agent_dispatch.delete_dispatch(dispatch.id, room_name)
            except api.TwirpError as err:
                if err.code == api.TwirpErrorCode.NOT_FOUND:
                    continue
                raise


def new_session_id() -> str:
    return uuid4().hex


def room_name_for_session(session_id: str) -> str:
    return f"friday-{session_id}"


def participant_identity_for_session(session_id: str) -> str:
    return f"web-{session_id}-{uuid4().hex[:8]}"
