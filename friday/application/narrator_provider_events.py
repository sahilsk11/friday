"""Provider event ingestion for narrator workflows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from friday.application.narrator_state import NarrationState
from friday.application.narrator_turns import ProviderFinalRecord
from friday.domain.repositories import (
    NarratorRepository,
    StoredNarratorEvent,
    StoredSession,
)
from friday.domain.state import AgentState


class ProviderEventIngestor:
    def __init__(
        self,
        *,
        store: NarratorRepository,
        get_narration_state: Callable[[str], NarrationState],
        schedule_progress: Callable[..., None],
        cancel_progress: Callable[[str], None],
        record_provider_final: Callable[..., ProviderFinalRecord | None],
        emit_final_for_text: Callable[..., Awaitable[StoredNarratorEvent | None]],
    ) -> None:
        self._store = store
        self._get_narration_state = get_narration_state
        self._schedule_progress = schedule_progress
        self._cancel_progress = cancel_progress
        self._record_provider_final = record_provider_final
        self._emit_final_for_text = emit_final_for_text

    async def on_text_delta(self, stored: StoredSession, text: str) -> None:
        if not text:
            return
        state = self._get_narration_state(stored.id)
        state.partial_assistant_text = (state.partial_assistant_text + text)[-2000:]

    async def on_text_final(self, stored: StoredSession, text: str) -> None:
        if not text:
            return
        self._cancel_progress(stored.id)
        state = self._get_narration_state(stored.id)
        provider_final = self._record_provider_final(
            stored=stored,
            final_text=text,
            active_turn_id=state.active_turn_id,
        )
        if provider_final is None:
            return
        await self._emit_final_for_text(
            stored,
            final_text=provider_final.final_text,
            turn_id=provider_final.turn_id,
            source="narrator_final",
            extra_payload={
                "provider_session_id": stored.provider_session_id,
                "provider_final_event_id": provider_final.provider_final_event_id,
            },
        )
        if state.active_turn_id == provider_final.turn_id:
            state.active_turn_id = None

    async def on_reasoning(self, stored: StoredSession, text: str) -> None:
        summary = _summarize_reasoning(text)
        if summary is None:
            return
        state = self._get_narration_state(stored.id)
        self._store.append_provider_event(
            session_id=stored.id,
            provider_session_id=stored.provider_session_id,
            event_type="activity",
            summary=summary,
            payload={
                "kind": "reasoning",
                "text": text,
                "turn_id": state.active_turn_id,
            },
        )
        self._schedule_progress(stored, delay=0.0, replace=True)

    async def on_state(self, stored: StoredSession, state: AgentState) -> None:
        self._store.append_provider_event(
            session_id=stored.id,
            provider_session_id=stored.provider_session_id,
            event_type="state",
            summary=state.value,
            payload={"state": state.value},
        )
        self._store.append_narrator_event(
            session_id=stored.id,
            event_type="state",
            payload={"state": state.value},
        )

    async def on_tool_start(
        self,
        stored: StoredSession,
        name: str,
        input_data: dict[str, Any],
    ) -> None:
        summary = _summarize_tool_start(name, input_data)
        state = self._get_narration_state(stored.id)
        self._store.append_provider_event(
            session_id=stored.id,
            provider_session_id=stored.provider_session_id,
            event_type="activity",
            summary=summary,
            payload={
                "kind": "tool",
                "name": name,
                "input": input_data,
                "input_summary": _summarize_tool_input(name, input_data),
                "turn_id": state.active_turn_id,
            },
        )
        self._schedule_progress(stored, delay=0.0, replace=True)

    async def on_error(self, stored: StoredSession, message: str) -> None:
        self._cancel_progress(stored.id)
        state = self._get_narration_state(stored.id)
        self._store.append_provider_event(
            session_id=stored.id,
            provider_session_id=stored.provider_session_id,
            event_type="error",
            summary=message,
            payload={"turn_id": state.active_turn_id},
        )
        if state.active_turn_id is not None:
            self._store.update_turn_status(
                turn_id=state.active_turn_id,
                status="error",
                error=message,
            )
            state.active_turn_id = None
        self._store.append_narrator_event(
            session_id=stored.id,
            event_type="error",
            text=message,
        )


__all__ = ["ProviderEventIngestor"]


def _summarize_reasoning(text: str) -> str | None:
    compact = " ".join(text.strip().split())
    if not compact:
        return None
    if len(compact) <= 420:
        return compact
    return f"{compact[:417].rstrip()}..."


def _summarize_tool_start(name: str, input_data: dict[str, Any]) -> str:
    if name == "task":
        description = _string_from_path(input_data, "description")
        prompt = _string_from_path(input_data, "prompt")
        if description:
            return f"Delegating: {description}."
        if prompt:
            return _sentence_from_prefix("Delegating a focused check", prompt)
        return "Delegating a focused project check."
    if name in {"grep", "glob"}:
        pattern = _first_string(input_data, ("pattern", "query", "path", "include"))
        if pattern:
            return f"Searching the project for {pattern!r}."
        return "Searching the project for relevant code."
    if name == "read":
        path = _first_string(input_data, ("filePath", "file_path", "path"))
        if path:
            return f"Reading {path}."
        return "Reading a relevant project file."
    if name == "webfetch":
        url = _first_string(input_data, ("url", "URL"))
        if url:
            return f"Checking external documentation at {url}."
        return "Checking external documentation."
    if name == "websearch":
        query = _first_string(input_data, ("query", "q"))
        if query:
            return f"Searching external references for {query!r}."
        return "Searching external references."
    if name == "bash":
        command = _first_string(input_data, ("command", "cmd"))
        if command:
            return f"Running {command!r}."
        return "Running a project command."
    if name in {"write", "edit"}:
        path = _first_string(input_data, ("filePath", "file_path", "path"))
        if path:
            return f"Editing {path}."
        return "Editing project files."
    if isinstance(input_data, dict) and input_data:
        return "Working through a project step."
    return "Continuing the task."


def _summarize_tool_input(name: str, input_data: dict[str, Any]) -> dict[str, str]:
    keys_by_tool = {
        "task": ("description", "prompt"),
        "grep": ("pattern", "query", "path", "include"),
        "glob": ("pattern", "path"),
        "read": ("filePath", "file_path", "path"),
        "webfetch": ("url", "prompt"),
        "websearch": ("query", "q"),
        "bash": ("command", "cmd"),
        "write": ("filePath", "file_path", "path"),
        "edit": ("filePath", "file_path", "path"),
    }
    summary: dict[str, str] = {}
    for key in keys_by_tool.get(name, ()):
        value = _string_from_path(input_data, key)
        if value:
            summary[key] = _trim(value, 500)
    return summary


def _first_string(input_data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _string_from_path(input_data, key)
        if value:
            return _trim(value, 220)
    return None


def _string_from_path(input_data: dict[str, Any], key: str) -> str | None:
    value = input_data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _sentence_from_prefix(prefix: str, detail: str) -> str:
    compact = _trim(" ".join(detail.split()), 220)
    return f"{prefix}: {compact}."


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3].rstrip()}..."
