"""Durable narrator turn lifecycle operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from friday.domain.repositories import NarratorRepository, StoredSession, StoredTurn

logger = logging.getLogger("friday.narrator")


@dataclass(frozen=True, slots=True)
class ProviderFinalRecord:
    turn_id: str
    final_text: str
    provider_final_event_id: int


class NarratorTurnLifecycle:
    def __init__(self, *, store: NarratorRepository) -> None:
        self._store = store

    def start_turn(
        self,
        *,
        stored: StoredSession,
        turn_id: str,
        user_text: str,
        source: str,
    ) -> None:
        self._store.append_message(
            session_id=stored.id,
            role="user",
            content=user_text,
            source=source,
        )
        self._store.create_turn(
            turn_id=turn_id,
            session_id=stored.id,
            provider_session_id=stored.provider_session_id,
            user_text=user_text,
            source=source,
        )

    def record_provider_final(
        self,
        *,
        stored: StoredSession,
        final_text: str,
        active_turn_id: str | None,
    ) -> ProviderFinalRecord | None:
        turn_id = self._resolve_turn_id(stored=stored, active_turn_id=active_turn_id)
        if turn_id is None:
            logger.warning(
                "dropping provider final without active turn | session=%s provider_session=%s",
                stored.id,
                stored.provider_session_id,
            )
            return None

        turn = self._store.get_turn(turn_id)
        if turn is None:
            logger.warning(
                "dropping provider final for missing turn | session=%s turn=%s",
                stored.id,
                turn_id,
            )
            return None
        if turn.provider_final_event_id is not None:
            if turn.provider_final_text and turn.provider_final_text != final_text:
                logger.warning(
                    "ignoring duplicate provider final with changed text | session=%s turn=%s",
                    stored.id,
                    turn.id,
                )
            return ProviderFinalRecord(
                turn_id=turn.id,
                final_text=turn.provider_final_text or final_text,
                provider_final_event_id=turn.provider_final_event_id,
            )

        provider_event = self._store.append_provider_event(
            session_id=stored.id,
            provider_session_id=stored.provider_session_id,
            event_type="final",
            summary=final_text,
            payload={"turn_id": turn.id},
        )
        self._store.mark_turn_provider_final(
            turn_id=turn.id,
            provider_final_text=final_text,
            provider_final_event_id=provider_event.id,
        )
        return ProviderFinalRecord(
            turn_id=turn.id,
            final_text=final_text,
            provider_final_event_id=provider_event.id,
        )

    def _resolve_turn_id(
        self,
        *,
        stored: StoredSession,
        active_turn_id: str | None,
    ) -> str | None:
        if active_turn_id is not None:
            turn = self._store.get_turn(active_turn_id)
            if _is_active_provider_turn(
                turn=turn,
                session_id=stored.id,
                provider_session_id=stored.provider_session_id,
            ):
                return active_turn_id
            return None

        turn = self._store.latest_active_turn(
            session_id=stored.id,
            provider_session_id=stored.provider_session_id,
        )
        return turn.id if turn is not None else None


def _is_active_provider_turn(
    *,
    turn: StoredTurn | None,
    session_id: str,
    provider_session_id: str,
) -> bool:
    if turn is None:
        return False
    if turn.session_id != session_id or turn.provider_session_id != provider_session_id:
        return False
    return turn.status not in {"completed", "cancelled", "error"}


__all__ = ["NarratorTurnLifecycle", "ProviderFinalRecord"]
