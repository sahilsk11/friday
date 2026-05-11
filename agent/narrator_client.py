from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from friday.application.voice import NarratorEvent


class HttpNarratorBackendClient:
    def __init__(self, base_url: str) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def submit_turn(
        self,
        *,
        session_id: str,
        source: str = "voice",
        text: str,
    ) -> list[NarratorEvent]:
        response = await self._http.post(
            f"/api/narrator/sessions/{session_id}/turns",
            json={"text": text, "source": source},
        )
        response.raise_for_status()
        payload = response.json()
        return [_parse_event(row) for row in payload.get("events", [])]

    async def cancel(self, *, session_id: str) -> list[NarratorEvent]:
        response = await self._http.post(f"/api/narrator/sessions/{session_id}/cancel")
        response.raise_for_status()
        payload = response.json()
        return [_parse_event(row) for row in payload.get("events", [])]

    async def list_events(
        self,
        *,
        session_id: str,
        after_id: int,
        limit: int = 50,
    ) -> list[NarratorEvent]:
        response = await self._http.get(
            f"/api/narrator/sessions/{session_id}/events",
            params={"after_id": after_id, "limit": limit},
        )
        response.raise_for_status()
        payload = response.json()
        return [_parse_event(row) for row in payload.get("events", [])]


def _parse_event(row: dict[str, Any]) -> NarratorEvent:
    payload = row.get("payload")
    created_at = row.get("created_at")
    return NarratorEvent(
        id=int(row["id"]),
        type=str(row["type"]),
        text=row.get("text"),
        payload=payload if isinstance(payload, dict) else {},
        created_at=datetime.fromisoformat(created_at) if isinstance(created_at, str) else None,
    )


NarratorClient = HttpNarratorBackendClient


__all__ = ["HttpNarratorBackendClient", "NarratorClient"]
