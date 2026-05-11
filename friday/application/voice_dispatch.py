"""Voice dispatch preparation use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from friday.domain.repositories import SessionRepository


@dataclass(frozen=True, slots=True)
class PreparedVoiceDispatch:
    session_id: str
    room_name: str
    harness: str
    model_id: str | None


class VoiceDispatchPreparationError(Exception):
    """Base error for voice dispatch preparation failures."""


class VoiceDispatchSessionNotFoundError(VoiceDispatchPreparationError):
    """Raised when no stored session exists for voice dispatch."""


class VoiceDispatchRoomMismatchError(VoiceDispatchPreparationError):
    """Raised when the requested room does not belong to the session."""


class VoiceDispatchPreparationService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        room_name_for_session: Callable[[str], str],
    ) -> None:
        self._sessions = sessions
        self._room_name_for_session = room_name_for_session

    def prepare(self, *, session_id: str, room_name: str) -> PreparedVoiceDispatch:
        stored = self._sessions.get_session(session_id)
        if stored is None:
            raise VoiceDispatchSessionNotFoundError(session_id)

        expected_room_name = self._room_name_for_session(stored.id)
        legacy_room_prefix = f"{expected_room_name}--"
        if room_name != expected_room_name and not room_name.startswith(legacy_room_prefix):
            raise VoiceDispatchRoomMismatchError(room_name)

        return PreparedVoiceDispatch(
            session_id=stored.id,
            room_name=room_name,
            harness=stored.harness,
            model_id=stored.model_id,
        )


__all__ = [
    "PreparedVoiceDispatch",
    "VoiceDispatchPreparationError",
    "VoiceDispatchPreparationService",
    "VoiceDispatchRoomMismatchError",
    "VoiceDispatchSessionNotFoundError",
]
