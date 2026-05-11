"""Narrator final-response recovery workflows."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from friday.domain.provider import Provider
from friday.domain.repositories import NarratorRepository, StoredNarratorEvent, StoredSession

logger = logging.getLogger("friday.narrator")

_FINAL_RECOVERY_MIN_AGE_SECS = 15.0


class NarratorRecoveryService:
    def __init__(
        self,
        *,
        store: NarratorRepository,
        require_provider: Callable[[str], Provider],
        emit_final_for_text: Callable[..., Awaitable[StoredNarratorEvent | None]],
    ) -> None:
        self._store = store
        self._require_provider = require_provider
        self._emit_final_for_text = emit_final_for_text

    async def recover_missing_final(self, stored: StoredSession) -> None:
        await self._recover_provider_finals_from_transcript(stored)
        for turn in self._store.recoverable_turns(
            session_id=stored.id,
            min_provider_final_age_seconds=_FINAL_RECOVERY_MIN_AGE_SECS,
        ):
            if not turn.provider_final_text:
                continue
            await self._emit_final_for_text(
                stored,
                final_text=turn.provider_final_text,
                turn_id=turn.id,
                source="narrator_final_recovery",
                extra_payload={
                    "provider_session_id": stored.provider_session_id,
                    "recovered_provider_event_id": turn.provider_final_event_id,
                },
            )

        latest_provider_final = next(
            (
                event
                for event in reversed(
                    self._store.list_provider_events(session_id=stored.id, limit=50)
                )
                if event.type == "final" and event.summary and event.summary.strip()
            ),
            None,
        )
        if latest_provider_final is None:
            return
        if not latest_provider_final.summary:
            return
        final_age_secs = (datetime.now(tz=UTC) - latest_provider_final.created_at).total_seconds()
        if final_age_secs < _FINAL_RECOVERY_MIN_AGE_SECS:
            return
        has_narrator_final = any(
            event.type == "final" and event.created_at >= latest_provider_final.created_at
            for event in self._store.list_narrator_events(
                session_id=stored.id,
                after_id=0,
                limit=200,
            )
        )
        if has_narrator_final:
            return
        logger.warning(
            "recovering legacy missing narrator final | session=%s provider_event=%s",
            stored.id,
            latest_provider_final.id,
        )
        await self._emit_final_for_text(
            stored,
            final_text=latest_provider_final.summary,
            turn_id=None,
            source="narrator_final_recovery",
            extra_payload={
                "provider_session_id": stored.provider_session_id,
                "recovered_provider_event_id": latest_provider_final.id,
            },
        )

    async def _recover_provider_finals_from_transcript(self, stored: StoredSession) -> None:
        missing_turns = self._store.turns_missing_provider_final(
            session_id=stored.id,
            min_age_seconds=_FINAL_RECOVERY_MIN_AGE_SECS,
        )
        if not missing_turns:
            return
        provider = self._require_provider(stored.harness)
        try:
            transcript = await provider.get_transcript(stored.provider_session_id)
        except Exception as err:
            logger.warning(
                "failed to recover provider transcript | session=%s err=%s",
                stored.id,
                err,
            )
            return
        assistant_messages = [
            message
            for message in transcript
            if message.role == "assistant"
            and message.text.strip()
            and message.completed_at is not None
        ]
        for turn in missing_turns:
            message = next(
                (
                    candidate
                    for candidate in assistant_messages
                    if candidate.completed_at is not None
                    and candidate.completed_at >= turn.created_at
                ),
                None,
            )
            if message is None:
                continue
            provider_event = self._store.append_provider_event(
                session_id=stored.id,
                provider_session_id=stored.provider_session_id,
                event_type="final",
                summary=message.text,
                payload={"turn_id": turn.id, "source": "provider_transcript_recovery"},
            )
            self._store.mark_turn_provider_final(
                turn_id=turn.id,
                provider_final_text=message.text,
                provider_final_event_id=provider_event.id,
            )


__all__ = ["NarratorRecoveryService"]
