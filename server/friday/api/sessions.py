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
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from friday.core.session_manager import Message, SessionInfo, SessionManager
from friday.core.state import AgentState

router = APIRouter(prefix="/sessions", tags=["sessions"])

# Keep-alive cadence for idle SSE streams. Browsers/proxies will drop a stream
# that's silent for too long; 15s is comfortably under the usual 60s timeout.
_SSE_KEEPALIVE_SECONDS = 15.0
# Bound the per-stream buffer. If the consumer is slow we drop the connection
# rather than ballooning memory — voice/UI clients can re-attach via GET.
_SSE_QUEUE_LIMIT = 256


class CreateSessionBody(BaseModel):
    title: str | None = None


class TurnBody(BaseModel):
    text: str


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
    session = await manager.create(title=body.title)
    info = await manager.get(session.id)
    return SessionRow.from_info(info)


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, manager: ManagerDep) -> SessionDetail:
    info = await manager.get(session_id)
    transcript = await manager.get_transcript(session_id)
    return SessionDetail(
        session=SessionRow.from_info(info),
        transcript=[MessageRow.from_message(m) for m in transcript],
    )


@router.post("/{session_id}/turn", status_code=202)
async def post_turn(session_id: str, body: TurnBody, manager: ManagerDep) -> dict[str, str]:
    session = manager.attach(session_id)
    await session.send_turn(body.text)
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
