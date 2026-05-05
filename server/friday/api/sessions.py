"""FastAPI router for session CRUD + SSE event stream.

Endpoints:

- ``GET    /sessions``                — list, optional ``?directory=`` filter
- ``POST   /sessions``                — create new (also creates opencode session)
- ``GET    /sessions/{id}``           — metadata + transcript
- ``POST   /sessions/{id}/turn``      — text turn (voice layer also POSTs here)
- ``GET    /sessions/{id}/events``    — SSE stream of live deltas + state

The router is framework-neutral: **no pipecat imports.** Both the CLI (Step 4)
and the voice pipeline (Step 5) drive the agent through this surface.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from friday.core.provider import ModelChoice
from friday.core.session_manager import Message, SessionInfo, SessionManager
from friday.core.state import AgentState

router = APIRouter(prefix="/sessions", tags=["sessions"])
models_router = APIRouter(tags=["models"])

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
    def from_choice(cls, choice: ModelChoice) -> ModelRef:
        return cls(providerID=choice.provider_id, modelID=choice.model_id)


class ModelInfo(BaseModel):
    providerID: str  # noqa: N815
    providerName: str  # noqa: N815
    modelID: str  # noqa: N815
    modelName: str  # noqa: N815


class ModelsResponse(BaseModel):
    models: list[ModelInfo]
    default: ModelRef | None


class CreateSessionBody(BaseModel):
    title: str | None = None
    directory: str | None = None


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
    def from_info(cls, info: SessionInfo) -> SessionRow:
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
    def from_message(cls, m: Message) -> MessageRow:
        return cls(
            role=m.role,
            text=m.text,
            completed_at=m.completed_at.isoformat() if m.completed_at else None,
        )


class SessionDetail(BaseModel):
    session: SessionRow
    transcript: list[MessageRow]
    current_model: ModelRef | None
    # Snapshot of the live agent state from the cached OpencodeSession —
    # ``thinking`` if opencode is mid-turn right now, ``idle`` otherwise.
    # Lets a freshly loaded page seed the thinking indicator without
    # waiting for the next opencode transition over the WS.
    agent_state: AgentState


def get_manager(request: Request) -> SessionManager:
    """FastAPI dependency. Tests override this to inject a manager directly."""
    manager: SessionManager | None = getattr(request.app.state, "manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="session manager not ready")
    return manager


ManagerDep = Annotated[SessionManager, Depends(get_manager)]


@router.get("", response_model=list[SessionRow])
async def list_sessions(
    manager: ManagerDep,
    directory: str | None = None,
) -> list[SessionRow]:
    sessions = await manager.list_sessions(directory=directory)
    return [SessionRow.from_info(s) for s in sessions]


@router.post("", response_model=SessionRow, status_code=201)
async def create_session(body: CreateSessionBody, manager: ManagerDep) -> SessionRow:
    if body.directory is not None:
        # Validate up-front: opencode launches tools that resolve paths
        # against this cwd, so a bogus directory means every tool call
        # fails downstream with a confusing error. We share a filesystem
        # with opencode (same process box), so a local stat is correct.
        if not os.path.isabs(body.directory):
            raise HTTPException(status_code=400, detail="directory must be an absolute path")
        if not await asyncio.to_thread(os.path.isdir, body.directory):
            raise HTTPException(
                status_code=400, detail=f"directory does not exist: {body.directory}"
            )
    session = await manager.create(title=body.title, directory=body.directory)
    info = await manager.get(session.id)
    return SessionRow.from_info(info)


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, manager: ManagerDep) -> SessionDetail:
    info = await manager.get(session_id)
    transcript = await manager.get_transcript(session_id)
    # ``current_model`` reports what opencode actually ran most recently —
    # ground truth, not intent. The user's pending selection lives in client
    # state and rides along on the next turn body.
    last_model = next(
        (m.model for m in reversed(transcript) if m.role == "assistant" and m.model is not None),
        None,
    )
    # ``agent_state`` is the live snapshot from the cached OpencodeSession,
    # not historical. ``manager.attach`` is a cache lookup — same instance
    # the voice pipeline observes, so the state is fresh.
    session = manager.attach(session_id)
    return SessionDetail(
        session=SessionRow.from_info(info),
        transcript=[MessageRow.from_message(m) for m in transcript],
        current_model=ModelRef.from_choice(last_model) if last_model else None,
        agent_state=session.current_state,
    )


@router.post("/{session_id}/turn", status_code=202)
async def post_turn(session_id: str, body: TurnBody, manager: ManagerDep) -> dict[str, str]:
    session = manager.attach(session_id)
    await session.send_turn(body.text, model=body.model.to_choice() if body.model else None)
    return {"session_id": session_id}


@router.get("/{session_id}/events")
async def stream_events(
    session_id: str,
    manager: ManagerDep,
) -> StreamingResponse:
    session = manager.attach(session_id)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_SSE_QUEUE_LIMIT)

    async def on_delta(text: str) -> None:
        await queue.put(_pack("text.delta", {"text": text}))

    async def on_final(text: str) -> None:
        await queue.put(_pack("text.final", {"text": text}))

    async def on_state(state: AgentState) -> None:
        await queue.put(_pack("state", {"state": state.value}))

    # NOTE: observers stay registered for the lifetime of the OpencodeSession.
    # Each new SSE connection adds 3 handlers; for v1 with a single client per
    # session that's fine. Step 7 (auth + multi-client) revisits this.
    session.on_text_delta(on_delta)
    session.on_text_final(on_final)
    session.on_state(on_state)

    return StreamingResponse(_sse_stream(queue), media_type="text/event-stream")


@models_router.get("/models", response_model=ModelsResponse)
async def list_models(manager: ManagerDep) -> ModelsResponse:
    """Proxy to opencode's ``GET /config/providers``, filtered to active +
    toolcall-capable models (anything else won't drive the agent).

    ``default`` is opencode's globally-configured default for whichever
    provider it points at; the UI uses this as the modal's pre-selection.
    """
    resp = await manager.http.get("/config/providers")
    resp.raise_for_status()
    payload = resp.json()
    providers = payload.get("providers") or []
    out: list[ModelInfo] = []
    for prov in providers:
        provider_id = prov.get("id", "")
        provider_name = prov.get("name", provider_id)
        for model_id, model in (prov.get("models") or {}).items():
            caps = model.get("capabilities") or {}
            if model.get("status") != "active":
                continue
            if not caps.get("toolcall"):
                continue
            out.append(
                ModelInfo(
                    providerID=provider_id,
                    providerName=provider_name,
                    modelID=model_id,
                    modelName=model.get("name", model_id),
                )
            )
    # opencode's `default` is `{ providerID: modelID, ... }` — pick whichever
    # provider it surfaces first as the global default.
    default_map: dict[str, str] = payload.get("default") or {}
    default: ModelRef | None = None
    for provider_id, model_id in default_map.items():
        default = ModelRef(providerID=provider_id, modelID=model_id)
        break
    return ModelsResponse(models=out, default=default)


async def _sse_stream(queue: asyncio.Queue[str]) -> AsyncIterator[bytes]:
    """Yield SSE frames from queue; emit a comment on idle to keep proxies open.

    Disconnects raise ``CancelledError`` from ``queue.get()``, which Starlette
    catches and the generator exits cleanly.
    """
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
