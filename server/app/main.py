from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from friday.application.sessions import (
    CurrentModelResult,
    SessionDetailResult,
    SessionNotFoundError,
    SessionQueryService,
    SessionSummaryResult,
    TranscriptEntryResult,
)
from friday.application.voice_dispatch import (
    VoiceDispatchPreparationService,
    VoiceDispatchRoomMismatchError,
    VoiceDispatchSessionNotFoundError,
)
from friday.domain.provider import Provider
from friday.domain.provider_registry import ProviderRegistry
from friday.domain.repositories import StoredNarratorEvent
from server.app.composition import build_application_state
from server.app.config import Settings, get_settings
from server.app.harness_model_defaults import default_model_ref, model_info_ref
from server.app.livekit_tokens import (
    create_friday_room,
    create_room_token,
    ensure_friday_agent_dispatch,
    new_session_id,
    participant_identity_for_session,
    room_name_for_session,
)
from server.app.narrator import NarratorManager
from server.app.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    CurrentModel,
    EnsureVoiceAgentRequest,
    EnsureVoiceAgentResponse,
    HarnessInfo,
    HealthResponse,
    ModelInfo,
    ModelsResponse,
    NarratorEventResponse,
    NarratorEventsResponse,
    NarratorTurnRequest,
    SessionDetailResponse,
    SessionSummary,
    TranscriptEntry,
    UpdateSessionRequest,
)

load_dotenv()

logger = logging.getLogger("friday.server")

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _pick_provider(registry: ProviderRegistry, harness: str | None) -> Provider:
    if harness is not None:
        p = registry.get(harness)
        if p is None:
            raise HTTPException(status_code=400, detail=f"unknown harness: {harness!r}")
        return p
    providers = registry.all()
    if not providers:
        raise HTTPException(status_code=503, detail="no provider available")
    return providers[0]


def _session_summary_response(session: SessionSummaryResult) -> SessionSummary:
    return SessionSummary(
        id=session.id,
        title=session.title,
        directory=session.directory,
        harness=session.harness,
        model_id=session.model_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _narrator_event_response(event: StoredNarratorEvent) -> NarratorEventResponse:
    return NarratorEventResponse(
        id=event.id,
        type=event.type,
        text=event.text,
        payload=event.payload,
        created_at=event.created_at,
    )


def _current_model_response(model: CurrentModelResult | None) -> CurrentModel | None:
    if model is None:
        return None
    return CurrentModel(provider_id=model.provider_id, model_id=model.model_id)


def _transcript_entry_response(entry: TranscriptEntryResult) -> TranscriptEntry:
    return TranscriptEntry(
        role=entry.role,
        text=entry.text,
        completed_at=entry.completed_at,
        error=entry.error,
        parts=entry.parts,
        model=_current_model_response(entry.model),
    )


def _session_detail_response(detail: SessionDetailResult) -> SessionDetailResponse:
    return SessionDetailResponse(
        session=_session_summary_response(detail.session),
        transcript=[_transcript_entry_response(entry) for entry in detail.transcript],
        narrator_transcript=[
            _transcript_entry_response(entry) for entry in detail.narrator_transcript
        ],
        current_model=_current_model_response(detail.current_model),
        agent_state=detail.agent_state,
    )


def _web_dist_path(settings: Settings) -> Path:
    return Path(settings.friday_web_dist).expanduser().resolve()


def _mount_web_assets(app: FastAPI, settings: Settings) -> None:
    dist = _web_dist_path(settings)
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")
    elif dist.exists():
        logger.warning("Friday web assets directory is missing | path=%s", assets)


def _web_file_response(settings: Settings, raw_path: str) -> FileResponse:
    dist = _web_dist_path(settings)
    index = dist / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Friday web build not found")

    requested = (dist / raw_path).resolve()
    if requested.is_file() and requested.is_relative_to(dist):
        return FileResponse(requested)
    return FileResponse(index)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    application_state = await build_application_state(settings)
    app.state.registry = application_state.registry
    app.state.narrator_store = application_state.store
    app.state.narrator_manager = application_state.narrator_manager
    app.state.session_query_service = application_state.session_query_service
    app.state.voice_dispatch_preparation_service = (
        application_state.voice_dispatch_preparation_service
    )
    try:
        yield
    finally:
        await application_state.aclose()


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry | None = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="provider not ready")
    return registry


RegistryDep = Annotated[object, Depends(get_registry)]


def get_narrator_manager(request: Request) -> NarratorManager:
    manager: NarratorManager | None = getattr(request.app.state, "narrator_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="narrator not ready")
    return manager


def get_session_query_service(request: Request) -> SessionQueryService:
    service: SessionQueryService | None = getattr(
        request.app.state,
        "session_query_service",
        None,
    )
    if service is None:
        raise HTTPException(status_code=503, detail="session query service not ready")
    return service


def get_voice_dispatch_preparation_service(request: Request) -> VoiceDispatchPreparationService:
    service: VoiceDispatchPreparationService | None = getattr(
        request.app.state,
        "voice_dispatch_preparation_service",
        None,
    )
    if service is None:
        raise HTTPException(status_code=503, detail="voice dispatch service not ready")
    return service


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Friday LiveKit PoC API", lifespan=lifespan)
    _mount_web_assets(app, settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "PATCH", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(ok=True)

    @app.get("/api/harnesses", response_model=list[HarnessInfo])
    async def list_harnesses(request: Request) -> list[HarnessInfo]:
        registry = get_registry(request)
        return [
            HarnessInfo(id=p.provider_id, name=registry.provider_name(p.provider_id))
            for p in registry.all()
        ]

    @app.get("/api/models", response_model=ModelsResponse)
    async def list_models(request: Request, harness: str | None = None) -> ModelsResponse:
        registry = get_registry(request)
        provider = _pick_provider(registry, harness)
        catalog = await provider.list_models()
        return ModelsResponse(
            models=[
                ModelInfo(
                    model_ref=model_info_ref(m),
                    provider_id=m.provider_id,
                    provider_name=m.provider_name,
                    model_id=m.model_id,
                    model_name=m.model_name,
                )
                for m in catalog.models
            ],
            default=default_model_ref(provider.provider_id, catalog),
        )

    @app.get("/api/sessions", response_model=list[SessionSummary])
    async def list_sessions(request: Request) -> list[SessionSummary]:
        service = get_session_query_service(request)
        return [_session_summary_response(session) for session in await service.list_sessions()]

    @app.get("/api/sessions/{session_id}", response_model=SessionDetailResponse)
    async def get_session_detail(session_id: str, request: Request) -> SessionDetailResponse:
        service = get_session_query_service(request)
        try:
            return _session_detail_response(await service.get_session_detail(session_id))
        except SessionNotFoundError as err:
            raise HTTPException(
                status_code=404,
                detail=f"unknown session: {session_id!r}",
            ) from err

    @app.patch("/api/sessions/{session_id}", response_model=SessionSummary)
    async def update_session(
        session_id: str,
        request: UpdateSessionRequest,
        fastapi_request: Request,
    ) -> SessionSummary:
        manager = get_narrator_manager(fastapi_request)
        try:
            stored = manager.rename_session(session_id=session_id, title=request.title)
        except KeyError as err:
            raise HTTPException(
                status_code=404,
                detail=f"unknown session: {session_id!r}",
            ) from err
        service = get_session_query_service(fastapi_request)
        detail = await service.get_session_detail(stored.id)
        return _session_summary_response(detail.session)

    @app.post("/api/sessions", response_model=CreateSessionResponse)
    async def create_session(
        request: CreateSessionRequest,
        current_settings: SettingsDep,
        fastapi_request: Request,
    ) -> CreateSessionResponse:
        manager = get_narrator_manager(fastapi_request)
        session_id = request.chat_id or new_session_id()
        try:
            stored = await manager.create_or_attach_session(
                session_id=session_id,
                harness=request.harness,
                model_id=request.model_id,
                title=request.title,
                directory=request.directory,
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        except RuntimeError as err:
            raise HTTPException(status_code=503, detail=str(err)) from err

        room_name = room_name_for_session(stored.id)
        participant_identity = participant_identity_for_session(stored.id)
        participant_name = request.participant_name or "Friday User"

        await create_friday_room(
            settings=current_settings,
            room_name=room_name,
            session_id=stored.id,
            harness=stored.harness,
            model_id=stored.model_id,
        )

        token = create_room_token(
            settings=current_settings,
            room_name=room_name,
            identity=participant_identity,
            name=participant_name,
        )

        return CreateSessionResponse(
            session_id=stored.id,
            room_name=room_name,
            participant_identity=participant_identity,
            participant_name=participant_name,
            livekit_url=current_settings.livekit_client_url,
            token=token,
            expires_in_seconds=current_settings.friday_token_ttl_seconds,
            harness=stored.harness,
            model_id=stored.model_id,
            title=stored.title,
            directory=stored.directory,
        )

    @app.post(
        "/api/sessions/{session_id}/voice-agent",
        response_model=EnsureVoiceAgentResponse,
    )
    async def ensure_voice_agent(
        session_id: str,
        request: EnsureVoiceAgentRequest,
        current_settings: SettingsDep,
        fastapi_request: Request,
    ) -> EnsureVoiceAgentResponse:
        service = get_voice_dispatch_preparation_service(fastapi_request)
        try:
            dispatch = service.prepare(session_id=session_id, room_name=request.room_name)
        except VoiceDispatchSessionNotFoundError as err:
            raise HTTPException(
                status_code=404,
                detail=f"unknown session: {session_id!r}",
            ) from err
        except VoiceDispatchRoomMismatchError as err:
            raise HTTPException(
                status_code=400,
                detail="room_name does not belong to this session",
            ) from err

        try:
            await ensure_friday_agent_dispatch(
                settings=current_settings,
                room_name=dispatch.room_name,
                session_id=dispatch.session_id,
                harness=dispatch.harness,
                model_id=dispatch.model_id,
            )
        except Exception as err:
            logger.warning(
                "failed to dispatch friday agent | session=%s room=%s err=%s",
                session_id,
                dispatch.room_name,
                err,
            )
            raise HTTPException(
                status_code=503,
                detail="unable to dispatch Friday agent to the room",
            ) from err

        return EnsureVoiceAgentResponse(dispatched=True, room_name=dispatch.room_name)

    @app.post(
        "/api/narrator/sessions/{session_id}/turns",
        response_model=NarratorEventsResponse,
    )
    async def submit_narrator_turn(
        session_id: str,
        request: NarratorTurnRequest,
        fastapi_request: Request,
    ) -> NarratorEventsResponse:
        manager = get_narrator_manager(fastapi_request)
        try:
            events = await manager.submit_user_turn(
                session_id=session_id,
                text=request.text,
                source=request.source,
            )
        except KeyError as err:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id!r}") from err
        return NarratorEventsResponse(
            events=[_narrator_event_response(event) for event in events],
        )

    @app.post(
        "/api/narrator/sessions/{session_id}/cancel",
        response_model=NarratorEventsResponse,
    )
    async def cancel_narrator_turn(
        session_id: str,
        fastapi_request: Request,
    ) -> NarratorEventsResponse:
        manager = get_narrator_manager(fastapi_request)
        try:
            event = await manager.cancel(session_id)
        except KeyError as err:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id!r}") from err
        return NarratorEventsResponse(events=[_narrator_event_response(event)])

    @app.post(
        "/api/narrator/sessions/{session_id}/recover-final",
        response_model=NarratorEventsResponse,
    )
    async def recover_narrator_final(
        session_id: str,
        fastapi_request: Request,
    ) -> NarratorEventsResponse:
        manager = get_narrator_manager(fastapi_request)
        try:
            await manager.recover_missing_final(session_id)
            events = manager.list_events(
                session_id=session_id,
                after_id=0,
                limit=100,
            )
        except KeyError as err:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id!r}") from err
        return NarratorEventsResponse(
            events=[_narrator_event_response(event) for event in events],
        )

    @app.get(
        "/api/narrator/sessions/{session_id}/events",
        response_model=NarratorEventsResponse,
    )
    async def list_narrator_events(
        session_id: str,
        fastapi_request: Request,
        after_id: int = 0,
        limit: int = 50,
    ) -> NarratorEventsResponse:
        manager = get_narrator_manager(fastapi_request)
        try:
            events = manager.list_events(
                session_id=session_id,
                after_id=max(after_id, 0),
                limit=min(max(limit, 1), 100),
            )
        except KeyError as err:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id!r}") from err
        return NarratorEventsResponse(
            events=[_narrator_event_response(event) for event in events],
        )

    @app.get("/", include_in_schema=False)
    async def web_index(current_settings: SettingsDep) -> FileResponse:
        return _web_file_response(current_settings, "")

    @app.get("/{path:path}", include_in_schema=False)
    async def web_fallback(path: str, current_settings: SettingsDep) -> FileResponse:
        if path == "healthz" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        return _web_file_response(current_settings, path)

    return app


app = create_app()
