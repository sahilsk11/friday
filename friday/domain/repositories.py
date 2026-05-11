"""Repository records and ports for Friday persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class StoredSession:
    id: str
    provider_session_id: str
    harness: str
    model_id: str | None
    title: str | None
    directory: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredNarratorEvent:
    id: int
    session_id: str
    type: str
    text: str | None
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredNarratorMessage:
    id: int
    session_id: str
    role: str
    content: str
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredProviderEvent:
    id: int
    session_id: str
    provider_session_id: str
    type: str
    summary: str | None
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredTurn:
    id: str
    session_id: str
    provider_session_id: str
    user_text: str
    source: str
    status: str
    provider_final_text: str | None
    narrator_final_text: str | None
    provider_final_event_id: int | None
    narrator_final_event_id: int | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class SessionRepository(Protocol):
    def upsert_session(
        self,
        *,
        session_id: str,
        provider_session_id: str,
        harness: str,
        model_id: str | None,
        title: str | None,
        directory: str | None,
    ) -> StoredSession: ...

    def get_session(self, session_id: str) -> StoredSession | None: ...

    def list_sessions(self) -> list[StoredSession]: ...

    def update_session_provider_session_id(
        self,
        *,
        session_id: str,
        provider_session_id: str,
    ) -> StoredSession: ...

    def update_session_title(
        self,
        *,
        session_id: str,
        title: str | None,
    ) -> StoredSession: ...


class NarratorMessageRepository(Protocol):
    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        source: str,
    ) -> None: ...

    def list_messages(self, session_id: str) -> list[StoredNarratorMessage]: ...


class NarratorEventRepository(Protocol):
    def append_narrator_event(
        self,
        *,
        session_id: str,
        event_type: str,
        text: str | None = None,
        payload: dict[str, Any] | None = None,
        event_key: str | None = None,
    ) -> StoredNarratorEvent: ...

    def get_narrator_event_by_key(
        self,
        *,
        session_id: str,
        event_key: str | None,
    ) -> StoredNarratorEvent | None: ...

    def list_narrator_events(
        self,
        *,
        session_id: str,
        after_id: int = 0,
        limit: int = 50,
    ) -> list[StoredNarratorEvent]: ...


class ProviderEventRepository(Protocol):
    def append_provider_event(
        self,
        *,
        session_id: str,
        provider_session_id: str,
        event_type: str,
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> StoredProviderEvent: ...

    def list_provider_events(
        self,
        *,
        session_id: str,
        limit: int = 10,
    ) -> list[StoredProviderEvent]: ...


class TurnRepository(Protocol):
    def create_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        provider_session_id: str,
        user_text: str,
        source: str,
    ) -> StoredTurn: ...

    def get_turn(self, turn_id: str) -> StoredTurn | None: ...

    def latest_turn(self, session_id: str) -> StoredTurn | None: ...

    def update_turn_status(
        self,
        *,
        turn_id: str,
        status: str,
        error: str | None = None,
    ) -> None: ...

    def mark_turn_provider_final(
        self,
        *,
        turn_id: str,
        provider_final_text: str,
        provider_final_event_id: int,
    ) -> None: ...

    def mark_turn_completed(
        self,
        *,
        turn_id: str,
        narrator_final_text: str,
        narrator_final_event_id: int,
    ) -> None: ...

    def recoverable_turns(
        self,
        *,
        session_id: str,
        min_provider_final_age_seconds: float,
        limit: int = 5,
    ) -> list[StoredTurn]: ...

    def turns_missing_provider_final(
        self,
        *,
        session_id: str,
        min_age_seconds: float,
        limit: int = 5,
    ) -> list[StoredTurn]: ...


class NarratorRepository(
    SessionRepository,
    NarratorMessageRepository,
    NarratorEventRepository,
    ProviderEventRepository,
    TurnRepository,
    Protocol,
):
    """Combined repository port used until application services are split."""


__all__ = [
    "NarratorEventRepository",
    "NarratorMessageRepository",
    "NarratorRepository",
    "ProviderEventRepository",
    "SessionRepository",
    "StoredNarratorEvent",
    "StoredNarratorMessage",
    "StoredProviderEvent",
    "StoredSession",
    "StoredTurn",
    "TurnRepository",
]
