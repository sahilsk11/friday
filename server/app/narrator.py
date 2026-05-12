from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from friday.application.narrator_progress import NarratorProgressScheduler
from friday.application.narrator_provider_events import ProviderEventIngestor
from friday.application.narrator_recovery import NarratorRecoveryService
from friday.application.narrator_snapshots import NarratorSnapshotBuilder
from friday.application.narrator_state import NarrationState
from friday.domain.provider import ModelCatalog, ModelChoice, Provider, ProviderSession, Unsubscribe
from friday.domain.provider_registry import ProviderRegistry
from friday.domain.repositories import (
    NarratorRepository,
    StoredNarratorEvent,
    StoredSession,
)
from friday.domain.state import AgentState
from server.app.harness_model_defaults import model_info_ref, parse_model_ref
from server.app.narrator_brain import EventedNarratorBrain, NarratorBrain

logger = logging.getLogger("friday.narrator")


@dataclass(slots=True)
class _ProviderBinding:
    session: ProviderSession
    unsubs: list[Unsubscribe] = field(default_factory=list)


class NarratorManager:
    """Backend-owned bridge between narrator turns and provider sessions."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        store: NarratorRepository,
        brain: NarratorBrain | None = None,
        progress_initial_delay_secs: float = 2.0,
        progress_cooldown_secs: float = 6.0,
    ) -> None:
        self._registry = registry
        self._store = store
        self._brain = brain or EventedNarratorBrain()
        self._bindings: dict[str, _ProviderBinding] = {}
        self._model_catalogs: dict[str, ModelCatalog] = {}
        self._narration_states: dict[str, NarrationState] = {}
        self._progress_initial_delay_secs = progress_initial_delay_secs
        self._progress_cooldown_secs = progress_cooldown_secs
        self._lock = asyncio.Lock()
        self._snapshot_builder = NarratorSnapshotBuilder(
            store=store,
            get_narration_state=self._narration_state,
            get_provider_state=self._current_provider_state,
        )
        self._progress = NarratorProgressScheduler(
            store=store,
            brain=self._brain,
            states=self._narration_states,
            get_narration_state=self._narration_state,
            build_snapshot=self._snapshot_builder.build,
            emit_speech=self._emit_speech,
            cooldown_secs=progress_cooldown_secs,
        )
        self._provider_events = ProviderEventIngestor(
            store=store,
            get_narration_state=self._narration_state,
            schedule_progress=self._progress.schedule,
            cancel_progress=self._progress.cancel,
            emit_final_for_text=self._emit_final_for_text,
        )
        self._recovery = NarratorRecoveryService(
            store=store,
            emit_final_for_text=self._emit_final_for_text,
        )

    async def aclose(self) -> None:
        async with self._lock:
            bindings = list(self._bindings.values())
            self._bindings.clear()
            states = list(self._narration_states.values())
            self._narration_states.clear()
        for state in states:
            if state.progress_task is not None:
                state.progress_task.cancel()
        for binding in bindings:
            for unsub in binding.unsubs:
                unsub()
        await self._brain.aclose()

    async def create_or_attach_session(
        self,
        *,
        session_id: str | None,
        harness: str | None,
        model_id: str | None,
        title: str | None,
        directory: str | None,
    ) -> StoredSession:
        stored = self._store.get_session(session_id) if session_id is not None else None
        if stored is not None:
            if harness is not None and harness != stored.harness:
                raise ValueError("session harness cannot be changed after creation")
            if model_id is not None and stored.model_id is not None and model_id != stored.model_id:
                raise ValueError("session model cannot be changed after creation")
            if title is not None and title != stored.title:
                stored = self._store.update_session_title(session_id=stored.id, title=title)
            await self._bind_provider(stored)
            return stored

        provider = await self._resolve_provider(session_id=None, harness=harness)
        provider_session = await provider.create_session(title=title, directory=directory)
        provider_session_id = provider_session.id
        if session_id is None:
            session_id = provider_session_id

        self._registry.register_session(provider_session_id, provider.provider_id)
        stored = self._store.upsert_session(
            session_id=session_id,
            provider_session_id=provider_session_id,
            harness=provider.provider_id,
            model_id=model_id,
            title=title,
            directory=directory,
        )
        await self._bind_provider(stored)
        return stored

    async def _record_provider_session_id(
        self,
        *,
        session_id: str,
        provider_id: str,
        provider_session_id: str,
    ) -> None:
        stored = self._store.get_session(session_id)
        if stored is None or stored.provider_session_id == provider_session_id:
            return
        updated = self._store.update_session_provider_session_id(
            session_id=session_id,
            provider_session_id=provider_session_id,
        )
        self._registry.register_session(provider_session_id, provider_id)
        logger.info(
            "provider session id updated | session=%s provider=%s provider_session=%s",
            updated.id,
            provider_id,
            updated.provider_session_id,
        )

    async def submit_user_turn(
        self,
        *,
        session_id: str,
        text: str,
        source: str = "voice",
    ) -> list[StoredNarratorEvent]:
        stored = self._require_session(session_id)
        provider = self._require_provider(stored.harness)
        binding = await self._bind_provider(stored)
        model = await self._resolve_model_choice(provider, stored.model_id)
        turn_id = uuid4().hex
        narration_state = self._narration_state(session_id)
        narration_state.latest_user_message = text
        narration_state.turn_started_at = time.monotonic()
        narration_state.has_spoken_this_turn = False
        narration_state.partial_assistant_text = ""
        narration_state.last_progress_activity_event_id = None
        narration_state.last_progress_tool_event_id = None
        narration_state.last_progress_reasoning_event_id = None
        narration_state.active_turn_id = turn_id

        self._store.append_message(
            session_id=session_id,
            role="user",
            content=text,
            source=source,
        )
        self._store.create_turn(
            turn_id=turn_id,
            session_id=session_id,
            provider_session_id=stored.provider_session_id,
            user_text=text,
            source=source,
        )

        try:
            await binding.session.send_turn(
                text,
                model=model,
                system=self._brain.provider_system,
            )
            self._store.update_turn_status(turn_id=turn_id, status="running")
        except Exception as err:
            self._store.update_turn_status(
                turn_id=turn_id,
                status="error",
                error=str(err),
            )
            raise
        self._progress.schedule(stored, delay=self._progress_initial_delay_secs)
        return []

    async def cancel(self, session_id: str) -> StoredNarratorEvent:
        stored = self._require_session(session_id)
        binding = await self._bind_provider(stored)
        await binding.session.cancel()
        self._progress.cancel(session_id)
        state = self._narration_state(session_id)
        if state.active_turn_id is not None:
            self._store.update_turn_status(
                turn_id=state.active_turn_id,
                status="cancelled",
            )
            state.active_turn_id = None
        return self._store.append_narrator_event(
            session_id=session_id,
            event_type="state",
            text=None,
            payload={"state": AgentState.IDLE.value},
        )

    def list_events(
        self,
        *,
        session_id: str,
        after_id: int,
        limit: int,
    ) -> list[StoredNarratorEvent]:
        self._require_session(session_id)
        return self._store.list_narrator_events(
            session_id=session_id,
            after_id=after_id,
            limit=limit,
        )

    def rename_session(self, *, session_id: str, title: str | None) -> StoredSession:
        self._require_session(session_id)
        return self._store.update_session_title(session_id=session_id, title=title)

    async def recover_missing_final(self, session_id: str) -> None:
        stored = self._require_session(session_id)
        await self._bind_provider(stored)
        await self._recovery.recover_missing_final(stored)

    async def _resolve_provider(self, *, session_id: str | None, harness: str | None) -> Provider:
        if harness is not None:
            provider = self._registry.get(harness)
            if provider is not None:
                return provider
            raise ValueError(f"unknown harness: {harness!r}")
        if session_id is not None:
            stored = self._store.get_session(session_id)
            if stored is not None:
                return self._require_provider(stored.harness)
            provider = await self._registry.resolve_for_session(session_id)
            if provider is not None:
                return provider
        providers = self._registry.all()
        if not providers:
            raise RuntimeError("no provider available")
        return providers[0]

    async def _bind_provider(self, stored: StoredSession) -> _ProviderBinding:
        async with self._lock:
            existing = self._bindings.get(stored.id)
            if existing is not None:
                return existing

            provider = self._require_provider(stored.harness)
            provider_session = provider.attach(stored.provider_session_id)
            binding = _ProviderBinding(session=provider_session)
            session_id = stored.id
            binding.unsubs.extend(
                [
                    provider_session.on_text_delta(
                        lambda text: self._provider_events.on_text_delta(
                            self._require_session(session_id), text
                        )
                    ),
                    provider_session.on_text_final(
                        lambda text: self._provider_events.on_text_final(
                            self._require_session(session_id), text
                        )
                    ),
                    provider_session.on_reasoning(
                        lambda text: self._provider_events.on_reasoning(
                            self._require_session(session_id), text
                        )
                    ),
                    provider_session.on_session_id(
                        lambda provider_session_id: self._record_provider_session_id(
                            session_id=session_id,
                            provider_id=provider.provider_id,
                            provider_session_id=provider_session_id,
                        )
                    ),
                    provider_session.on_state(
                        lambda state: self._provider_events.on_state(
                            self._require_session(session_id), state
                        )
                    ),
                    provider_session.on_tool_start(
                        lambda name, input_data: self._provider_events.on_tool_start(
                            self._require_session(session_id),
                            name,
                            input_data,
                        )
                    ),
                    provider_session.on_error(
                        lambda message: self._provider_events.on_error(
                            self._require_session(session_id), message
                        )
                    ),
                ]
            )
            self._bindings[stored.id] = binding
            return binding

    async def _emit_final_for_text(
        self,
        stored: StoredSession,
        *,
        final_text: str,
        turn_id: str | None,
        source: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> StoredNarratorEvent | None:
        if turn_id is None:
            logger.warning(
                "dropping narrator final without turn id | session=%s source=%s",
                stored.id,
                source,
            )
            return None
        snapshot = self._snapshot_builder.build(
            stored,
            decision_type="final_response",
            final_text=final_text,
            turn_id=turn_id,
        )
        try:
            decision = await asyncio.wait_for(self._brain.decide(snapshot), timeout=12.0)
        except TimeoutError:
            logger.warning("narrator final decision timed out | session=%s", stored.id)
            decision = await EventedNarratorBrain().decide(snapshot)
        if decision.action != "speak" or not decision.text:
            decision = await EventedNarratorBrain().decide(snapshot)
        if decision.action != "speak" or not decision.text:
            return None
        event = self._emit_speech(
            stored,
            event_type="final",
            text=decision.text,
            source=source,
            extra_payload={
                "turn_id": turn_id,
                **(extra_payload or {}),
            },
            event_key=f"turn:{turn_id}:final" if turn_id is not None else None,
        )
        if turn_id is not None:
            self._store.mark_turn_completed(
                turn_id=turn_id,
                narrator_final_text=decision.text,
                narrator_final_event_id=event.id,
            )
        return event

    def _narration_state(self, session_id: str) -> NarrationState:
        state = self._narration_states.get(session_id)
        if state is None:
            state = NarrationState()
            self._narration_states[session_id] = state
        return state

    def _emit_speech(
        self,
        stored: StoredSession,
        *,
        event_type: str,
        text: str,
        source: str,
        extra_payload: dict[str, Any] | None = None,
        event_key: str | None = None,
    ) -> StoredNarratorEvent:
        existing = self._store.get_narrator_event_by_key(
            session_id=stored.id,
            event_key=event_key,
        )
        if existing is not None:
            return existing
        state = self._narration_state(stored.id)
        state.last_spoken_at = time.monotonic()
        state.has_spoken_this_turn = True
        self._store.append_message(
            session_id=stored.id,
            role="narrator",
            content=text,
            source=source,
        )
        payload = {
            "source": source,
            "brain": type(self._brain).__name__,
            **(extra_payload or {}),
        }
        return self._store.append_narrator_event(
            session_id=stored.id,
            event_type=event_type,
            text=text,
            payload=payload,
            event_key=event_key,
        )

    def _current_provider_state(self, stored: StoredSession) -> AgentState:
        binding = self._bindings.get(stored.id)
        if binding is None:
            return AgentState.IDLE
        return binding.session.current_state

    async def _resolve_model_choice(
        self,
        provider: Provider,
        model_id: str | None,
    ) -> ModelChoice | None:
        if model_id is None:
            return None
        catalog = self._model_catalogs.get(provider.provider_id)
        if catalog is None:
            catalog = await provider.list_models()
            self._model_catalogs[provider.provider_id] = catalog
        requested_provider_id, requested_model_id = parse_model_ref(model_id)
        for model in catalog.models:
            if requested_provider_id is not None:
                if (
                    model.provider_id == requested_provider_id
                    and model.model_id == requested_model_id
                ):
                    return ModelChoice(provider_id=model.provider_id, model_id=model.model_id)
                continue
            if model.model_id == requested_model_id or model_info_ref(model) == model_id:
                return ModelChoice(provider_id=model.provider_id, model_id=model.model_id)
        return None

    def _require_session(self, session_id: str) -> StoredSession:
        stored = self._store.get_session(session_id)
        if stored is None:
            raise KeyError(session_id)
        return stored

    def _require_provider(self, harness: str) -> Provider:
        provider = self._registry.get(harness)
        if provider is None:
            raise RuntimeError(f"provider not available: {harness}")
        return provider


async def close_manager(manager: NarratorManager) -> None:
    with contextlib.suppress(Exception):
        await manager.aclose()
