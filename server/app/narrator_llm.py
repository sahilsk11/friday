"""Compatibility shim for narrator JSON chat clients."""

from __future__ import annotations

from friday.infra.narrator_llm.json_chat import (
    OpenAICompatibleJsonChatClient,
    OpenCodeServerJsonChatClient,
)

__all__ = ["OpenAICompatibleJsonChatClient", "OpenCodeServerJsonChatClient"]
