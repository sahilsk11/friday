"""FastAPI router for session CRUD + SSE event stream.

Endpoints:

- ``GET    /harnesses``               — list available provider backends
- ``GET    /models?harness=<id>``     — model catalog for a specific harness
- ``GET    /sessions``                — list, optional ``?directory=`` filter
- ``POST   /sessions``                — create new (harness + directory required)
- ``GET    /sessions/{id}``           — metadata + transcript
- ``POST   /sessions/{id}/turn``      — text turn
- ``GET    /sessions/{id}/events``    — SSE stream of live deltas + state

The router is framework-neutral: **no pipecat imports.** Both the CLI (Step 4)
and the voice pipeline (Step 5) drive the agent through this surface.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from friday.core.provider import Message, ModelChoice, Provider, SessionInfo, SessionNotFound
from friday.core.session_registry import ProviderRegistry
from friday.core.state import AgentState

router = APIRouter(prefix="/sessions", tags=["sessions"])
models_router = APIRouter(tags=["models"])
harnesses_router = APIRouter(tags=["harnesses"])

# Keep-alive cadence for idle SSE streams. Browsers/proxies will drop a stream
# that's silent for too long; 15s is comfortably under the usual 60s timeout.
_SSE_KEEPALIVE_SECONDS = 15.0
# Bound the per-stream buffer. If the consumer is slow we drop the connection
# rather than ballooning memory — voice/UI clients can re-attach via GET.
_SSE_QUEUE_LIMIT = 256


class ModelRef(BaseModel):
    """Wire shape for picking an opencode model. Matches both inbound API
    bodies and the ``model`` field that opencode's prompt endpoint accepts —
    so the field names are camelCase to mirror the wire, not Python style."""

    providerID: str  # noqa: N815
    modelID: str  # noqa: N815

    def to_choice(self) -> ModelChoice:
        return ModelChoice(provider_id=self.providerID, model_id=self.modelID)

    @classmethod
    def from_choice(cls, choice: ModelChoice) -> "ModelRef":
        return cls(providerID=choice.provider_id, modelID=choice.model_id)


class ModelInfo(BaseModel):
    providerID: str  # noqa: N815
    providerName: str  # noqa: N815
    modelID: str  # noqa: N815
    modelName: str  # noqa: N815


class ModelsResponse(BaseModel):
    models: list[ModelInfo]
    default: ModelRef | None


class HarnessInfo(BaseModel):
    id: str
    name: str


class TurnBody(BaseModel):
    text: str
    model: ModelRef | None = None


class SessionRow(BaseModel):
    id: str
    title: str
    directory: str
    created_at: str
    updated_at: str

    @classmethod
    def from_info(cls, info: SessionInfo) -> "SessionRow":
        return cls(
            id=info.id,
            title=info.title,
            directory=info.directory,
            created_at=info.created_at.isoformat(),
            updated_at=info.updated_at.isoformat(),
        )


class MessageRow(BaseModel):
    role: str
    text: str
    completed_at: str | None

    @classmethod
    def from_message(cls, m: Message) -> "MessageRow":
        return cls(
            role=m.role,
            text=m.text,
            completed_at=m.completed_at.isoformat() if m.completed_at else None,
        )


class SessionDetail(BaseModel):
    session: SessionRow
    transcript: list[MessageRow]
    current_model: ModelRef | None
    agent_state: AgentState


def get_registry(request: Request) -> ProviderRegistry:
    """FastAPI dependency. Tests override this to inject a registry directly."""
    registry: ProviderRegistry | None = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="provider not ready")
    return registry


# Keep the old name for backward compat with tests that still override get_provider.
# Tests should migrate to get_registry, but this avoids a hard break.
def get_provider(request: Request) -> Provider:
    registry = get_registry(request)
    providers = registry.all()
    if not providers:
        raise HTTPException(status_code=503, detail="no provider available")
    return providers[0]


RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]


def _pick_provider(registry: ProviderRegistry, harness: str | None) -> Provider:
    """Resolve a provider by harness id, falling back to the first available."""
    if harness is not None:
        p = registry.get(harness)
        if p is None:
            raise HTTPException(status_code=400, detail=f"unknown harness: {harness!r}")
        return p
    providers = registry.all()
    if not providers:
        raise HTTPException(status_code=503, detail="no provider available")
    return providers[0]


async def _require_provider_for_session(registry: ProviderRegistry, session_id: str) -> Provider:
    """Look up which provider owns a session; 404 if none claim it."""
    provider = await registry.resolve_for_session(session_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return provider


# ── Harnesses ──────────────────────────────────────────────────────────────


@harnesses_router.get("/harnesses", response_model=list[HarnessInfo])
async def list_harnesses(registry: RegistryDep) -> list[HarnessInfo]:
    """List all available provider backends (harnesses)."""
    return [
        HarnessInfo(id=p.provider_id, name=registry.provider_name(p.provider_id))
        for p in registry.all()
    ]


# ── Models ─────────────────────────────────────────────────────────────────


@models_router.get("/models", response_model=ModelsResponse)
async def list_models(registry: RegistryDep, harness: str | None = None) -> ModelsResponse:
    """The model picker. Pass ``?harness=<id>`` to get models for a specific
    provider; omit to get models for the first available provider."""
    provider = _pick_provider(registry, harness)
    catalog = await provider.list_models()
    return ModelsResponse(
        models=[
            ModelInfo(
                providerID=m.provider_id,
                providerName=m.provider_name,
                modelID=m.model_id,
                modelName=m.model_name,
            )
            for m in catalog.models
        ],
        default=ModelRef.from_choice(catalog.default) if catalog.default else None,
    )


# ── Sessions ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[SessionRow])
async def list_sessions(
    registry: RegistryDep,
    directory: str | None = None,
) -> list[SessionRow]:
    """List sessions across all providers, merged and sorted newest-first."""
    results = await asyncio.gather(
        *[p.list_sessions(directory=directory) for p in registry.all()],
        return_exceptions=True,
    )
    rows: list[SessionRow] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        rows.extend(SessionRow.from_info(info) for info in r)
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, registry: RegistryDep) -> SessionDetail:
    provider = await _require_provider_for_session(registry, session_id)
    try:
        info = await provider.get_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    transcript = await provider.get_transcript(session_id)
    last_model = next(
        (m.model for m in reversed(transcript) if m.role == "assistant" and m.model is not None),
        None,
    )
    session = provider.attach(session_id)
    return SessionDetail(
        session=SessionRow.from_info(info),
        transcript=[MessageRow.from_message(m) for m in transcript],
        current_model=ModelRef.from_choice(last_model) if last_model else None,
        agent_state=session.current_state,
    )


@router.post("/{session_id}/turn", status_code=202)
async def post_turn(
    session_id: str, body: TurnBody, registry: RegistryDep
) -> dict[str, str]:
    provider = await _require_provider_for_session(registry, session_id)
    session = provider.attach(session_id)
    await session.send_turn(body.text, model=body.model.to_choice() if body.model else None)
    return {"session_id": session_id}


@router.get("/{session_id}/events")
async def stream_events(
    session_id: str,
    registry: RegistryDep,
) -> StreamingResponse:
    provider = await _require_provider_for_session(registry, session_id)
    session = provider.attach(session_id)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_SSE_QUEUE_LIMIT)

    async def on_delta(text: str) -> None:
        await queue.put(_pack("text.delta", {"text": text}))

    async def on_final(text: str) -> None:
        await queue.put(_pack("text.final", {"text": text}))

    async def on_state(state: AgentState) -> None:
        await queue.put(_pack("state", {"state": state.value}))

    session.on_text_delta(on_delta)
    session.on_text_final(on_final)
    session.on_state(on_state)

    return StreamingResponse(_sse_stream(queue), media_type="text/event-stream")


async def _sse_stream(queue: asyncio.Queue[str]) -> AsyncIterator[bytes]:
    while True:
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
        except TimeoutError:
            yield b": keep-alive\n\n"
            continue
        yield chunk.encode("utf-8")


def _pack(event_type: str, payload: dict[str, str]) -> str:
    data = json.dumps({"type": event_type, **payload})
    return f"data: {data}\n\n"
