"""Narrator final-response recovery workflows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from friday.domain.repositories import NarratorRepository, StoredNarratorEvent, StoredSession

_FINAL_RECOVERY_MIN_AGE_SECS = 15.0


class NarratorRecoveryService:
    def __init__(
        self,
        *,
        store: NarratorRepository,
        emit_final_for_text: Callable[..., Awaitable[StoredNarratorEvent | None]],
    ) -> None:
        self._store = store
        self._emit_final_for_text = emit_final_for_text

    async def recover_missing_final(self, stored: StoredSession) -> list[StoredNarratorEvent]:
        recovered: list[StoredNarratorEvent] = []
        for turn in self._store.recoverable_turns(
            session_id=stored.id,
            min_provider_final_age_seconds=_FINAL_RECOVERY_MIN_AGE_SECS,
        ):
            if not turn.provider_final_text:
                continue
            event = await self._emit_final_for_text(
                stored,
                final_text=turn.provider_final_text,
                turn_id=turn.id,
                source="narrator_final_recovery",
                extra_payload={
                    "provider_session_id": stored.provider_session_id,
                    "recovered_provider_event_id": turn.provider_final_event_id,
                },
            )
            if event is not None:
                recovered.append(event)
        return recovered


__all__ = ["NarratorRecoveryService"]
