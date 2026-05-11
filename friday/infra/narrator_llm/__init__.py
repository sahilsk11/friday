"""Narrator LLM infrastructure clients."""

from friday.infra.narrator_llm.json_chat import (
    OpenAICompatibleJsonChatClient,
    OpenCodeServerJsonChatClient,
)

__all__ = ["OpenAICompatibleJsonChatClient", "OpenCodeServerJsonChatClient"]
