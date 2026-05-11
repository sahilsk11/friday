"""Narrator progress narration scheduling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from friday.application.narrator_state import NarrationState
from friday.domain.repositories import (
    NarratorRepository,
    StoredNarratorEvent,
    StoredProviderEvent,
    StoredSession,
)

logger = logging.getLogger("friday.narrator")


class ProgressNarratorBrain(Protocol):
    async def decide(self, snapshot: dict[str, Any]) -> Any: ...


class NarratorProgressScheduler:
    def __init__(
        self,
        *,
        store: NarratorRepository,
        brain: ProgressNarratorBrain,
        states: dict[str, NarrationState],
        get_narration_state: Callable[[str], NarrationState],
        build_snapshot: Callable[..., dict[str, Any]],
        emit_speech: Callable[..., StoredNarratorEvent],
        cooldown_secs: float,
    ) -> None:
        self._store = store
        self._brain = brain
        self._states = states
        self._get_narration_state = get_narration_state
        self._build_snapshot = build_snapshot
        self._emit_speech = emit_speech
        self._cooldown_secs = cooldown_secs
        self._long_silence_secs = max(20.0, cooldown_secs * 4)

    def schedule(
        self,
        stored: StoredSession,
        *,
        delay: float,
        replace: bool = False,
    ) -> None:
        state = self._get_narration_state(stored.id)
        if replace and state.progress_task is not None and not state.progress_task.done():
            state.progress_task.cancel()
            state.progress_task = None
        if state.progress_task is not None and not state.progress_task.done():
            return
        state.progress_task = asyncio.create_task(
            self._run(stored, delay=delay),
            name=f"friday-narrator-progress-{stored.id}",
        )

    def cancel(self, session_id: str) -> None:
        state = self._states.get(session_id)
        if state is None or state.progress_task is None:
            return
        state.progress_task.cancel()
        state.progress_task = None

    async def _run(self, stored: StoredSession, *, delay: float) -> None:
        try:
            next_delay = delay
            while True:
                if next_delay > 0:
                    await asyncio.sleep(next_delay)
                text = None
                if self._should_speak_now(stored):
                    decision = await self._brain.decide(
                        self._build_snapshot(stored, decision_type="progress_check")
                    )
                    text = decision.text or self._fallback_progress_text(stored)
                    self._mark_latest_activity_spoken(stored)
                else:
                    text = None
                if text:
                    self._emit_speech(
                        stored,
                        event_type="progress",
                        text=text,
                        source="narrator_progress",
                    )
                next_delay = self._cooldown_secs
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("narrator progress check failed | session=%s err=%s", stored.id, err)
        finally:
            state = self._states.get(stored.id)
            if state is not None and state.progress_task is asyncio.current_task():
                state.progress_task = None

    def _should_speak_now(self, stored: StoredSession) -> bool:
        state = self._get_narration_state(stored.id)
        latest_activity = self._latest_activity_event(stored)
        now = time.monotonic()
        if latest_activity is not None:
            if latest_activity.id in {
                state.last_progress_activity_event_id,
                state.last_progress_tool_event_id,
                state.last_progress_reasoning_event_id,
            }:
                return False
            if (
                state.last_spoken_at is not None
                and now - state.last_spoken_at < self._cooldown_secs
            ):
                return False
            return True
        if state.last_spoken_at is None and state.turn_started_at is not None:
            return now - state.turn_started_at >= self._long_silence_secs
        if state.last_spoken_at is not None:
            return now - state.last_spoken_at >= self._long_silence_secs
        return False

    def _fallback_progress_text(self, stored: StoredSession) -> str | None:
        state = self._get_narration_state(stored.id)
        latest_activity = self._latest_activity_event(stored)
        if latest_activity is None:
            return "The agent is still working, but has not produced a detailed update yet."
        state.last_progress_activity_event_id = latest_activity.id
        if latest_activity.summary:
            return _spoken_progress_summary(latest_activity.summary)
        return None

    def _mark_latest_activity_spoken(self, stored: StoredSession) -> None:
        latest_activity = self._latest_activity_event(stored)
        if latest_activity is None:
            return
        state = self._get_narration_state(stored.id)
        state.last_progress_activity_event_id = latest_activity.id

    def _latest_activity_event(self, stored: StoredSession) -> StoredProviderEvent | None:
        return next(
            (
                event
                for event in reversed(
                    self._store.list_provider_events(session_id=stored.id, limit=10)
                )
                if event.type == "activity"
            ),
            None,
        )


__all__ = ["NarratorProgressScheduler"]


def _spoken_progress_summary(summary: str | None) -> str | None:
    if summary is None:
        return None
    compact = " ".join(summary.strip().split())
    if not compact:
        return None
    compact = compact.replace("Let me ", "I'm going to ", 1)
    compact = compact.replace("I need to ", "I'm ", 1)
    if len(compact) <= 220:
        return compact
    sentence_end = compact.find(". ")
    if 20 <= sentence_end <= 220:
        return compact[: sentence_end + 1]
    return f"{compact[:217].rstrip()}..."
