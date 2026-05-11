"""Session query use cases."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from friday.domain.provider_registry import ProviderRegistry
from friday.domain.repositories import (
    NarratorMessageRepository,
    SessionRepository,
    StoredSession,
)

logger = logging.getLogger("friday.sessions")


@dataclass(frozen=True, slots=True)
class CurrentModelResult:
    provider_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class TranscriptEntryResult:
    role: str
    text: str
    completed_at: datetime | None
    error: str | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    model: CurrentModelResult | None = None


@dataclass(frozen=True, slots=True)
class SessionSummaryResult:
    id: str
    title: str | None
    directory: str | None
    harness: str
    model_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SessionDetailResult:
    session: SessionSummaryResult
    transcript: list[TranscriptEntryResult]
    narrator_transcript: list[TranscriptEntryResult]
    current_model: CurrentModelResult | None
    agent_state: str


class SessionNotFoundError(Exception):
    """Raised when no stored or provider-owned session exists for a session id."""


class SessionQueryService:
    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        sessions: SessionRepository,
        narrator_messages: NarratorMessageRepository,
    ) -> None:
        self._registry = registry
        self._sessions = sessions
        self._narrator_messages = narrator_messages

    async def list_sessions(self) -> list[SessionSummaryResult]:
        sessions_by_id: dict[str, SessionSummaryResult] = {}
        stored_by_provider_session_id: dict[str, StoredSession] = {}

        for stored_session in self._sessions.list_sessions():
            stored_by_provider_session_id[stored_session.provider_session_id] = stored_session
            sessions_by_id[stored_session.id] = _stored_session_summary(
                stored_session,
                title=self._display_title(stored_session),
            )

        for provider in self._registry.all():
            try:
                provider_sessions = await provider.list_sessions()
            except Exception as err:
                logger.warning(
                    "failed to list sessions for provider %s | err=%s",
                    provider.provider_id,
                    err,
                )
                continue

            for session in provider_sessions:
                self._registry.register_session(session.id, provider.provider_id)
                stored_for_provider = stored_by_provider_session_id.get(session.id)
                session_key = stored_for_provider.id if stored_for_provider else session.id
                sessions_by_id[session_key] = SessionSummaryResult(
                    id=session_key,
                    harness=(
                        stored_for_provider.harness if stored_for_provider else provider.provider_id
                    ),
                    title=self._display_title(
                        stored_for_provider,
                        provider_title=session.title,
                        harness=provider.provider_id,
                        created_at=session.created_at,
                    ),
                    directory=(
                        stored_for_provider.directory
                        if stored_for_provider and stored_for_provider.directory
                        else session.directory
                    ),
                    model_id=stored_for_provider.model_id if stored_for_provider else None,
                    created_at=session.created_at,
                    updated_at=max(
                        session.updated_at,
                        (
                            stored_for_provider.updated_at
                            if stored_for_provider
                            else session.updated_at
                        ),
                    ),
                )

        return sorted(
            sessions_by_id.values(),
            key=lambda session: session.updated_at,
            reverse=True,
        )[:100]

    async def get_session_detail(self, session_id: str) -> SessionDetailResult:
        stored = self._sessions.get_session(session_id)
        provider = await self._registry.resolve_for_session(session_id)
        if provider is None and stored is not None:
            provider = self._registry.get(stored.harness)

        if provider is None:
            if stored is None:
                raise SessionNotFoundError(session_id)
            return SessionDetailResult(
                session=_stored_session_summary(stored, title=self._display_title(stored)),
                transcript=[],
                narrator_transcript=self._narrator_transcript(session_id),
                current_model=(
                    CurrentModelResult(provider_id=stored.harness, model_id=stored.model_id)
                    if stored.model_id
                    else None
                ),
                agent_state="idle",
            )

        provider_session_id = stored.provider_session_id if stored is not None else session_id
        try:
            info = await provider.get_session(provider_session_id)
        except Exception as err:
            if stored is not None:
                recovered_provider_session_id = await self._recover_provider_session_id(
                    provider=provider,
                    stored=stored,
                )
                if recovered_provider_session_id is not None:
                    provider_session_id = recovered_provider_session_id
                    info = await provider.get_session(provider_session_id)
                else:
                    info = None
            else:
                raise SessionNotFoundError(session_id) from err

        transcript_messages = (
            await provider.get_transcript(provider_session_id) if info is not None else []
        )
        model_from_transcript = next(
            (
                CurrentModelResult(
                    provider_id=message.model.provider_id,
                    model_id=message.model.model_id,
                )
                for message in reversed(transcript_messages)
                if message.model is not None
            ),
            None,
        )
        current_model = model_from_transcript
        if current_model is None and stored and stored.model_id:
            current_model = CurrentModelResult(
                provider_id=stored.harness,
                model_id=stored.model_id,
            )

        if info is not None:
            session_summary = SessionSummaryResult(
                id=stored.id if stored else info.id,
                harness=stored.harness if stored else provider.provider_id,
                title=self._display_title(
                    stored,
                    provider_title=info.title,
                    harness=stored.harness if stored else provider.provider_id,
                    created_at=info.created_at,
                ),
                directory=(stored.directory if stored and stored.directory else info.directory),
                model_id=(
                    current_model.model_id
                    if current_model
                    else (stored.model_id if stored else None)
                ),
                created_at=info.created_at,
                updated_at=max(
                    info.updated_at,
                    stored.updated_at if stored else info.updated_at,
                ),
            )
        else:
            assert stored is not None
            session_summary = _stored_session_summary(stored, title=self._display_title(stored))

        live_session = provider.attach(provider_session_id)
        return SessionDetailResult(
            session=session_summary,
            transcript=[
                TranscriptEntryResult(
                    role=message.role,
                    text=message.text,
                    completed_at=message.completed_at,
                    error=message.error,
                    parts=message.parts,
                    model=(
                        CurrentModelResult(
                            provider_id=message.model.provider_id,
                            model_id=message.model.model_id,
                        )
                        if message.model is not None
                        else None
                    ),
                )
                for message in transcript_messages
            ],
            narrator_transcript=self._narrator_transcript(session_id),
            current_model=current_model,
            agent_state=live_session.current_state.value,
        )

    def _narrator_transcript(self, session_id: str) -> list[TranscriptEntryResult]:
        return [
            TranscriptEntryResult(
                role=message.role,
                text=message.content,
                completed_at=message.created_at,
                error=None,
            )
            for message in self._narrator_messages.list_messages(session_id)
        ]

    async def _recover_provider_session_id(
        self,
        *,
        provider: Any,
        stored: StoredSession,
    ) -> str | None:
        first_user_message = next(
            (
                message.content.strip()
                for message in self._narrator_messages.list_messages(stored.id)
                if message.role == "user" and message.content.strip()
            ),
            "",
        )
        if not first_user_message:
            return None

        needle = _matchable_text(first_user_message)
        if len(needle) < 40:
            return None

        try:
            candidates = await provider.list_sessions(directory=stored.directory)
        except Exception as err:
            logger.debug(
                "failed to list provider sessions for recovery | session=%s err=%s",
                stored.id,
                err,
            )
            return None

        for candidate in candidates[:25]:
            if candidate.id == stored.provider_session_id:
                continue
            try:
                transcript = await provider.get_transcript(candidate.id)
            except Exception:
                continue
            transcript_text = _matchable_text(
                "\n".join(message.text for message in transcript if message.role == "user")
            )
            if needle in transcript_text:
                updated = self._sessions.update_session_provider_session_id(
                    session_id=stored.id,
                    provider_session_id=candidate.id,
                )
                logger.info(
                    "recovered provider session id | session=%s provider_session=%s",
                    updated.id,
                    updated.provider_session_id,
                )
                return str(candidate.id)
        return None

    def _display_title(
        self,
        stored: StoredSession | None,
        *,
        provider_title: str | None = None,
        harness: str | None = None,
        created_at: datetime | None = None,
    ) -> str:
        if stored is not None and stored.title:
            return stored.title

        if stored is not None:
            for message in self._narrator_messages.list_messages(stored.id):
                if message.role == "user" and message.content.strip():
                    return _summarize_title(message.content)

        clean_provider_title = _clean_provider_title(provider_title)
        if clean_provider_title is not None:
            return clean_provider_title

        effective_harness = (stored.harness if stored is not None else harness) or "session"
        effective_created_at = (
            stored.created_at if stored is not None else created_at
        ) or datetime.now()
        provider_label = _provider_label(effective_harness)
        title_time = _format_title_time(effective_created_at)
        return f"{provider_label} session - {title_time}"


def _stored_session_summary(
    session: StoredSession,
    *,
    title: str | None = None,
) -> SessionSummaryResult:
    return SessionSummaryResult(
        id=session.id,
        title=title if title is not None else session.title,
        directory=session.directory,
        harness=session.harness,
        model_id=session.model_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _summarize_title(text: str) -> str:
    compact = " ".join(text.strip().split())
    if not compact:
        return "Untitled session"
    words = compact.split(" ")
    summary = " ".join(words[:10])
    if len(summary) > 72:
        summary = f"{summary[:69].rstrip()}..."
    elif len(words) > 10:
        summary = f"{summary}..."
    return summary


def _matchable_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _clean_provider_title(title: str | None) -> str | None:
    if title is None:
        return None
    stripped = title.strip()
    if not stripped:
        return None
    if stripped.startswith("rollout-"):
        return None
    if len(stripped) >= 24 and all(char in "0123456789abcdefABCDEF-" for char in stripped):
        return None
    return stripped


def _provider_label(provider_id: str) -> str:
    if provider_id == "codex":
        return "Codex"
    if provider_id == "opencode":
        return "OpenCode"
    return provider_id.capitalize()


def _format_title_time(value: datetime) -> str:
    return value.strftime("%b %-d, %-I:%M %p")


__all__ = [
    "CurrentModelResult",
    "SessionDetailResult",
    "SessionNotFoundError",
    "SessionQueryService",
    "SessionSummaryResult",
    "TranscriptEntryResult",
]
