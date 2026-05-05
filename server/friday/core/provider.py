"""Provider abstraction for LLM backends.

One abstraction layer supporting multiple backends:
- OpenCode (existing, via HTTP + SSE)
- ClaudeCode (via Anthropic Agent SDK)

Each provider exposes the same interface: sessions, send_turn, cancel,
and observer callbacks for text deltas, text final, state, tool start.
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from friday.core.state import AgentState


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """One model selection."""

    provider_id: str
    model_id: str

    def to_wire(self) -> dict[str, str]:
        return {"providerID": self.provider_id, "modelID": self.model_id}


EventHandler = Callable[[Any], Awaitable[None]]
TextDeltaHandler = Callable[[str], Awaitable[None]]
TextFinalHandler = Callable[[str], Awaitable[None]]
StateHandler = Callable[[AgentState], Awaitable[None]]
ToolStartHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
Unsubscribe = Callable[[], None]

T = TypeVar("T")


@runtime_checkable
class Provider(Protocol):
    """Abstract LLM provider interface.

    Concrete implementations wrap different backends (OpenCode, ClaudeCode, etc.)
    but expose the same session-based API.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique provider identifier (e.g., "opencode", "claude-code")."""
        ...

    @abstractmethod
    async def create_session(self, title: str | None = None) -> ProviderSession:
        """Create a new session."""
        ...

    @abstractmethod
    async def aclose(self) -> None:
        """Clean up provider resources."""
        ...


@runtime_checkable
class ProviderSession(Protocol):
    """One provider session. Observer-based API mirrors OpencodeSession."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Session identifier."""
        ...

    @property
    @abstractmethod
    def current_state(self) -> AgentState:
        """Latest agent state. Defaults to IDLE."""
        ...

    @abstractmethod
    def on_text_delta(self, handler: TextDeltaHandler) -> Unsubscribe:
        """Subscribe to streaming text deltas."""
        ...

    @abstractmethod
    def on_text_final(self, handler: TextFinalHandler) -> Unsubscribe:
        """Subscribe to final text when turn completes."""
        ...

    @abstractmethod
    def on_state(self, handler: StateHandler) -> Unsubscribe:
        """Subscribe to state changes (THINKING → IDLE)."""
        ...

    @abstractmethod
    def on_tool_start(self, handler: ToolStartHandler) -> Unsubscribe:
        """Subscribe to tool invocations."""
        ...

    @abstractmethod
    async def send_turn(
        self,
        text: str,
        model: ModelChoice | None = None,
        *,
        system: str | None = None,
    ) -> None:
        """Send a turn to the session."""
        ...

    @abstractmethod
    async def cancel(self) -> None:
        """Abort in-flight turn."""
        ...


def _subscribe[H](handlers: list[H], handler: H) -> Unsubscribe:
    """Append a handler and return a function that removes it."""
    handlers.append(handler)

    def unsubscribe() -> None:
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    return unsubscribe