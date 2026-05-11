"""Provider abstraction for LLM backends."""

from __future__ import annotations

import contextlib
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from friday.domain.state import AgentState


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """A specific model selection."""

    provider_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One row in the model picker."""

    provider_id: str
    provider_name: str
    model_id: str
    model_name: str


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """The model picker payload: selectable models plus an optional default."""

    models: list[ModelInfo]
    default: ModelChoice | None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One row in the sessions list."""

    id: str
    title: str
    directory: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    """One message in a transcript."""

    role: str
    text: str
    completed_at: datetime | None
    parts: list[dict[str, Any]] = field(default_factory=list)
    model: ModelChoice | None = None
    error: str | None = None


EventHandler = Callable[[Any], Awaitable[None]]
TextDeltaHandler = Callable[[str], Awaitable[None]]
TextFinalHandler = Callable[[str], Awaitable[None]]
ReasoningHandler = Callable[[str], Awaitable[None]]
SessionIdHandler = Callable[[str], Awaitable[None]]
StateHandler = Callable[[AgentState], Awaitable[None]]
ToolStartHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ErrorHandler = Callable[[str], Awaitable[None]]
Unsubscribe = Callable[[], None]

T = TypeVar("T")
H = TypeVar("H")


@runtime_checkable
class Provider(Protocol):
    """Abstract LLM provider interface."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique provider identifier, such as "opencode" or "codex"."""
        ...

    @abstractmethod
    async def create_session(
        self,
        title: str | None = None,
        *,
        directory: str | None = None,
    ) -> ProviderSession:
        """Create a new session."""
        ...

    @abstractmethod
    def attach(self, session_id: str) -> ProviderSession:
        """Return the live session for an existing id."""
        ...

    @abstractmethod
    async def list_sessions(
        self,
        *,
        directory: str | None = None,
    ) -> list[SessionInfo]:
        """List sessions, optionally filtered to one working directory."""
        ...

    @abstractmethod
    async def get_session(self, session_id: str) -> SessionInfo:
        """Fetch metadata for one session."""
        ...

    @abstractmethod
    async def get_transcript(self, session_id: str) -> list[Message]:
        """Fetch the full transcript, ordered oldest-first."""
        ...

    @abstractmethod
    async def list_models(self) -> ModelCatalog:
        """Models the backend can run, plus an optional default."""
        ...

    @abstractmethod
    async def aclose(self) -> None:
        """Clean up provider resources."""
        ...


@runtime_checkable
class ProviderSession(Protocol):
    """One provider session with an observer-based API."""

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
    def on_reasoning(self, handler: ReasoningHandler) -> Unsubscribe:
        """Subscribe to provider reasoning updates when the backend exposes them."""
        ...

    @abstractmethod
    def on_session_id(self, handler: SessionIdHandler) -> Unsubscribe:
        """Subscribe to provider-native session id changes discovered after startup."""
        ...

    @abstractmethod
    def on_state(self, handler: StateHandler) -> Unsubscribe:
        """Subscribe to state changes."""
        ...

    @abstractmethod
    def on_tool_start(self, handler: ToolStartHandler) -> Unsubscribe:
        """Subscribe to tool invocations."""
        ...

    @abstractmethod
    def on_error(self, handler: ErrorHandler) -> Unsubscribe:
        """Subscribe to provider-reported turn errors."""
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


class SessionNotFound(Exception):
    """Raised by a provider when a session_id has no matching session."""


def subscribe(handlers: list[H], handler: H) -> Unsubscribe:
    """Append a handler and return a function that removes it."""
    handlers.append(handler)

    def unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            handlers.remove(handler)

    return unsubscribe


__all__ = [
    "ErrorHandler",
    "EventHandler",
    "Message",
    "ModelCatalog",
    "ModelChoice",
    "ModelInfo",
    "Provider",
    "ProviderSession",
    "ReasoningHandler",
    "SessionIdHandler",
    "SessionInfo",
    "SessionNotFound",
    "StateHandler",
    "TextDeltaHandler",
    "TextFinalHandler",
    "ToolStartHandler",
    "Unsubscribe",
    "subscribe",
]
