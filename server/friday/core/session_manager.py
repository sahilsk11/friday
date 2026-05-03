"""SessionManager — typed wrapper over opencode's session HTTP API.

Opencode owns persistence; friday is a thin façade. This module exposes typed
``SessionInfo`` / ``Message`` objects (built from raw JSON) and routes live
``OpencodeSession`` lookups through :class:`OpencodeClient`'s existing cache so
event observers can attach without duplicating state.

Wire shapes are pinned by ``scripts/probe_session_manager.py`` against a real
opencode 1.14 server; if opencode bumps the schema, the probe is the canary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from friday.core.opencode_session import OpencodeClient, OpencodeSession


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Flattened ``GET /session`` / ``GET /session/:id`` row."""

    id: str
    title: str
    directory: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    """Flattened ``GET /session/:id/message`` row.

    ``text`` concatenates all ``type == "text"`` parts in order — that's what
    ends up rendered to the user. ``parts`` keeps the raw list so callers that
    care about tool invocations or step boundaries can inspect them.
    """

    role: str
    text: str
    completed_at: datetime | None
    parts: list[dict[str, Any]] = field(default_factory=list)


class SessionManager:
    """Typed-domain wrapper over :class:`OpencodeClient`'s HTTP surface."""

    def __init__(self, client: OpencodeClient) -> None:
        self._client = client

    async def list_sessions(self, *, directory: str | None = None) -> list[SessionInfo]:
        """List all sessions, optionally filtered to one working directory."""
        resp = await self._client.http.get("/session")
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json()
        sessions = [_parse_session_info(row) for row in rows]
        if directory is not None:
            sessions = [s for s in sessions if s.directory == directory]
        return sessions

    async def get(self, session_id: str) -> SessionInfo:
        """Fetch metadata for one session."""
        resp = await self._client.http.get(f"/session/{session_id}")
        resp.raise_for_status()
        return _parse_session_info(resp.json())

    async def get_transcript(self, session_id: str) -> list[Message]:
        """Fetch the full transcript for a session, ordered oldest-first."""
        resp = await self._client.http.get(f"/session/{session_id}/message")
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json()
        return [_parse_message(row) for row in rows]

    async def create(self, title: str | None = None) -> OpencodeSession:
        """Create a new session and return its live wrapper."""
        return await self._client.new_session(title)

    def attach(self, session_id: str) -> OpencodeSession:
        """Return a live wrapper for an existing session (cached)."""
        return self._client.session(session_id)


def _parse_session_info(row: dict[str, Any]) -> SessionInfo:
    time = row.get("time") or {}
    return SessionInfo(
        id=row["id"],
        title=row.get("title", ""),
        directory=row.get("directory", ""),
        created_at=_ms_to_datetime(time.get("created", 0)),
        updated_at=_ms_to_datetime(time.get("updated", time.get("created", 0))),
    )


def _parse_message(row: dict[str, Any]) -> Message:
    info = row.get("info") or {}
    parts: list[dict[str, Any]] = row.get("parts") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    time = info.get("time") or {}
    completed_ms = time.get("completed") or time.get("end")
    return Message(
        role=info.get("role", ""),
        text=text,
        completed_at=_ms_to_datetime(completed_ms) if completed_ms else None,
        parts=parts,
    )


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
