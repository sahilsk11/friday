from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from friday.application.sessions import SessionQueryService
from friday.application.voice_dispatch import VoiceDispatchPreparationService
from friday.domain.provider_registry import ProviderRegistry
from friday.infra.persistence.sqlite_narrator_store import NarratorStore
from server.app.config import Settings
from server.app.livekit_tokens import room_name_for_session
from server.app.narrator import NarratorManager
from server.app.narrator_brain_factory import create_narrator_brain

logger = logging.getLogger("friday.server.composition")


@dataclass(slots=True)
class ApplicationState:
    registry: ProviderRegistry
    store: NarratorStore
    narrator_manager: NarratorManager
    session_query_service: SessionQueryService
    voice_dispatch_preparation_service: VoiceDispatchPreparationService

    async def aclose(self) -> None:
        await self.narrator_manager.aclose()
        for provider in self.registry.all():
            await provider.aclose()
        self.store.close()


async def build_application_state(settings: Settings) -> ApplicationState:
    registry = ProviderRegistry()
    store = NarratorStore(settings.friday_db_path)
    store.start()

    try:
        await _start_providers(registry, settings)
        if not registry.all():
            raise RuntimeError("no providers available")
    except Exception:
        store.close()
        raise

    try:
        brain = create_narrator_brain(
            settings.friday_narrator_brain,
            llm_provider=settings.friday_narrator_llm_provider,
            llm_base_url=settings.friday_narrator_llm_base_url,
            llm_api_key=settings.narrator_llm_api_key,
            llm_model=settings.friday_narrator_llm_model,
            opencode_base_url=settings.narrator_opencode_base_url,
            opencode_model=settings.friday_narrator_opencode_model,
            opencode_agent=settings.friday_narrator_opencode_agent,
            opencode_directory=settings.friday_narrator_opencode_directory,
            opencode_timeout_secs=settings.friday_narrator_opencode_timeout_secs,
            opencode_disable_tools=settings.friday_narrator_opencode_disable_tools,
            opencode_delete_sessions=settings.friday_narrator_opencode_delete_sessions,
        )
    except ValueError as err:
        for provider in registry.all():
            await provider.aclose()
        store.close()
        raise RuntimeError(str(err)) from err

    manager = NarratorManager(
        registry=registry,
        store=store,
        brain=brain,
        progress_initial_delay_secs=settings.friday_narrator_progress_initial_delay_secs,
        progress_cooldown_secs=settings.friday_narrator_progress_cooldown_secs,
    )
    session_queries = SessionQueryService(
        registry=registry,
        sessions=store,
        narrator_messages=store,
    )
    voice_dispatch_preparation = VoiceDispatchPreparationService(
        sessions=store,
        room_name_for_session=room_name_for_session,
    )
    return ApplicationState(
        registry=registry,
        store=store,
        narrator_manager=manager,
        session_query_service=session_queries,
        voice_dispatch_preparation_service=voice_dispatch_preparation,
    )


async def _start_providers(registry: ProviderRegistry, settings: Settings) -> None:
    opencode_base_url = settings.opencode_base_url
    if opencode_base_url:
        from friday.infra.providers.opencode import OpencodeProvider

        opencode = OpencodeProvider(opencode_base_url)
        try:
            await asyncio.wait_for(opencode.start(), timeout=5.0)
            registry.add(opencode)
            logger.info("opencode provider started | url=%s", opencode_base_url)
        except Exception as err:
            logger.warning("opencode provider unavailable, skipping | err=%s", err)
            await opencode.aclose()

    from friday.infra.providers.codex import CodexProvider

    codex = CodexProvider()
    registry.add(codex)
    logger.info("codex provider started")


__all__ = ["ApplicationState", "build_application_state"]
