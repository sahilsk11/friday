"""Narrator decision snapshot construction."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from friday.application.narrator_state import NarrationState
from friday.domain.repositories import NarratorRepository, StoredSession
from friday.domain.state import AgentState


def _round_seconds(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


class NarratorSnapshotBuilder:
    def __init__(
        self,
        *,
        store: NarratorRepository,
        get_narration_state: Callable[[str], NarrationState],
        get_provider_state: Callable[[StoredSession], AgentState],
    ) -> None:
        self._store = store
        self._get_narration_state = get_narration_state
        self._get_provider_state = get_provider_state

    def build(
        self,
        stored: StoredSession,
        *,
        decision_type: str,
        final_text: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.monotonic()
        state = self._get_narration_state(stored.id)
        messages = self._store.list_messages(stored.id)[-8:]
        provider_events = self._store.list_provider_events(session_id=stored.id, limit=8)
        spoken_context = [
            {
                "role": "friday" if message.role == "narrator" else message.role,
                "text": message.content,
            }
            for message in messages
        ]
        recent_events = [
            {
                "type": event.type,
                "summary": event.summary,
                "activity": _activity_payload_for_prompt(event.payload),
            }
            for event in provider_events
            if event.summary
        ]
        elapsed_since_user = (
            now - state.turn_started_at if state.turn_started_at is not None else None
        )
        elapsed_since_last_speech = (
            now - state.last_spoken_at if state.last_spoken_at is not None else None
        )
        return {
            "decision_type": decision_type,
            "turn_id": turn_id,
            "session_state": {
                "provider_state": self._get_provider_state(stored).value,
                "elapsed_since_user_seconds": _round_seconds(elapsed_since_user),
                "elapsed_since_last_speech_seconds": _round_seconds(elapsed_since_last_speech),
                "has_spoken_this_user_turn": state.has_spoken_this_turn,
            },
            "spoken_context": spoken_context,
            "latest_user_message": state.latest_user_message,
            "provider_context": {
                "recent_events": recent_events,
                "partial_assistant_text": state.partial_assistant_text,
                "final_text": final_text,
            },
        }


__all__ = ["NarratorSnapshotBuilder"]


def _activity_payload_for_prompt(payload: dict[str, Any]) -> dict[str, Any] | None:
    kind = payload.get("kind")
    if kind not in {"reasoning", "tool"}:
        return None
    activity: dict[str, Any] = {"kind": kind}
    if kind == "tool":
        name = payload.get("name")
        input_summary = payload.get("input_summary")
        if isinstance(name, str):
            activity["tool"] = name
        if isinstance(input_summary, dict):
            activity["input_summary"] = input_summary
    return activity
