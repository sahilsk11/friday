from __future__ import annotations

import logging

from friday.infra.narrator_llm.json_chat import (
    OpenAICompatibleJsonChatClient,
    OpenCodeServerJsonChatClient,
)
from server.app.narrator_brain import (
    EventedNarratorBrain,
    JsonChatClient,
    JsonNarratorBrain,
    NarratorBrain,
)

logger = logging.getLogger("friday.narrator_brain_factory")


def create_narrator_brain(
    kind: str,
    *,
    llm_provider: str = "openai_compatible",
    llm_base_url: str = "",
    llm_api_key: str = "",
    llm_model: str = "",
    opencode_base_url: str = "",
    opencode_model: str = "",
    opencode_agent: str = "",
    opencode_directory: str = "",
    opencode_timeout_secs: float = 15.0,
    opencode_disable_tools: bool = True,
    opencode_delete_sessions: bool = True,
) -> NarratorBrain:
    if kind in {"evented", "pass_through"}:
        return EventedNarratorBrain()
    if kind == "opencode_server":
        llm_provider = "opencode_server"
        kind = "llm"
    if kind in {"openai_compatible", "llm"}:
        chat_client: JsonChatClient
        if llm_provider == "opencode_server":
            if not opencode_base_url:
                logger.warning(
                    "opencode_server narrator requested without OPENCODE base URL; "
                    "using evented brain"
                )
                return EventedNarratorBrain()
            chat_client = OpenCodeServerJsonChatClient(
                base_url=opencode_base_url,
                model=opencode_model,
                agent=opencode_agent,
                directory=opencode_directory,
                timeout_secs=opencode_timeout_secs,
                disable_tools=opencode_disable_tools,
                delete_sessions=opencode_delete_sessions,
            )
            return JsonNarratorBrain(chat_client=chat_client)

        if llm_provider != "openai_compatible":
            raise ValueError(f"unsupported narrator LLM provider: {llm_provider!r}")
        if not llm_base_url or not llm_api_key or not llm_model:
            logger.warning(
                "openai_compatible narrator requested without full config; using evented brain"
            )
            return EventedNarratorBrain()
        chat_client = OpenAICompatibleJsonChatClient(
            base_url=llm_base_url,
            api_key=llm_api_key,
            model=llm_model,
        )
        return JsonNarratorBrain(chat_client=chat_client)
    raise ValueError(f"unsupported narrator brain: {kind!r}")
