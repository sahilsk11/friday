"""Provider abstraction for LLM backends.

One abstraction layer supporting multiple backends:
- OpenCode (existing, via HTTP + SSE)
- ClaudeCode (via Anthropic Agent SDK)

Each provider exposes the same interface: persistence (list/get sessions
and transcripts, list available models), creating live sessions, and the
session-level observer API (text deltas, text final, state, tool start).

Application code (api/, voice/, cli/) only ever holds these protocol
types. Backend specifics — HTTP wire shapes, SSE multiplexing, SDK calls,
file-backed session stores — stay inside each provider implementation.
"""

from __future__ import annotations

import contextlib
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from friday.core.state import AgentState


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """A specific model selection. Pure value type — no wire shape baked in.

    Each provider translates this into whatever its backend expects (opencode
    POSTs ``{providerID, modelID}``; the Anthropic SDK takes a single
    ``model_id`` string)."""

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
    """The model picker payload — selectable models plus an optional default."""

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
    """One message in a transcript.

    ``text`` is the rendered, user-visible content. ``parts`` keeps the
    backend-native shape so callers that care about tool invocations or
    structured parts can introspect. ``model`` is set on assistant messages
    when the backend records which model ran the turn — None for user
    messages or backends that don't track this. ``error`` is set when the
    backend reports a failure (e.g., API errors, rate limits)."""

    role: str
    text: str
    completed_at: datetime | None
    parts: list[dict[str, Any]] = field(default_factory=list)
    model: ModelChoice | None = None
    error: str | None = None


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

    Concrete implementations wrap different backends (OpenCode, ClaudeCode,
    …) but expose the same surface: live sessions, persistence (list/get
    sessions and transcripts), and a model catalog.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique provider identifier (e.g., "opencode", "claude-code")."""
        ...

    # ── Live sessions ──────────────────────────────────────────────────

    @abstractmethod
    async def create_session(
        self,
        title: str | None = None,
        *,
        directory: str | None = None,
    ) -> ProviderSession:
        """Create a new session.

        ``directory`` pins the working directory tools should resolve paths
        against. Providers that don't support per-session working directories
        may ignore it."""
        ...

    @abstractmethod
    def attach(self, session_id: str) -> ProviderSession:
        """Return the live session for an existing id.

        Cached: repeated calls return the same instance so multiple
        observers can attach to one session."""
        ...

    # ── Persistence ────────────────────────────────────────────────────

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

    # ── Lifecycle ──────────────────────────────────────────────────────

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


class SessionNotFound(Exception):
    """Raised by a provider when a session_id has no matching session."""


def subscribe[H](handlers: list[H], handler: H) -> Unsubscribe:
    """Append a handler and return a function that removes it."""
    handlers.append(handler)

    def unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            handlers.remove(handler)

    return unsubscribe
