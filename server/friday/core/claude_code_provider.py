"""ClaudeCode provider using Anthropic Agent SDK.

Wraps the Claude Agent SDK library to expose the same Provider + ProviderSession
interface as OpenCode.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolUseBlock,
    get_session_info,
    get_session_messages,
    list_sessions,
    query,
)
from loguru import logger

from friday.core.provider import (
    ErrorHandler,
    Message,
    ModelCatalog,
    ModelChoice,
    ModelInfo,
    SessionInfo,
    SessionNotFound,
    StateHandler,
    TextDeltaHandler,
    TextFinalHandler,
    ToolStartHandler,
    Unsubscribe,
    subscribe,
)
from friday.core.state import AgentState


@dataclass
class ClaudeCodeSession:
    """One Claude Code session wrapping the Agent SDK query generator.

    Maps SDK message types to the ProviderSession observer API:
    - SystemMessage(subtype="init") → captures session_id
    - StreamEvent (when enabled) → text deltas via raw event parsing
    - AssistantMessage → tool use detection
    - ResultMessage → text_final + state(IDLE)
    """

    _http: Any = field(repr=False)
    _query_task: asyncio.Task[None] | None = None
    _cancelled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _sdk_assignment_tasks: set[asyncio.Future[None]] = field(default_factory=set, repr=False)

    id: str = ""
    title: str | None = None
    directory: str | None = None
    current_state: AgentState = AgentState.IDLE

    # SDK-assigned UUID. Empty on pending sessions (before the first query() fires).
    # For cold-attached sessions, _sdk_id == id from the start.
    _sdk_id: str = ""
    # Called once when the SDK fires its first session_id (pending sessions only).
    _on_sdk_id_assigned: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False)

    _delta_handlers: list[TextDeltaHandler] = field(default_factory=list, repr=False)
    _final_handlers: list[TextFinalHandler] = field(default_factory=list, repr=False)
    _state_handlers: list[StateHandler] = field(default_factory=list, repr=False)
    _tool_start_handlers: list[ToolStartHandler] = field(default_factory=list, repr=False)
    _error_handlers: list[ErrorHandler] = field(default_factory=list, repr=False)

    _text_accumulated: str = ""
    _announced_tools: set[str] = field(default_factory=set, repr=False)

    def on_text_delta(self, handler: TextDeltaHandler) -> Unsubscribe:
        return subscribe(self._delta_handlers, handler)

    def on_text_final(self, handler: TextFinalHandler) -> Unsubscribe:
        return subscribe(self._final_handlers, handler)

    def on_state(self, handler: StateHandler) -> Unsubscribe:
        return subscribe(self._state_handlers, handler)

    def on_tool_start(self, handler: ToolStartHandler) -> Unsubscribe:
        return subscribe(self._tool_start_handlers, handler)

    def on_error(self, handler: ErrorHandler) -> Unsubscribe:
        return subscribe(self._error_handlers, handler)

    @property
    def sdk_id(self) -> str:
        return self._sdk_id

    async def send_turn(
        self,
        text: str,
        model: ModelChoice | None = None,
        *,
        system: str | None = None,
    ) -> None:
        if self._query_task is not None and not self._query_task.done():
            logger.warning("ClaudeCodeSession: already has a turn in-flight")
            return

        self._text_accumulated = ""
        self._announced_tools.clear()
        opts = ClaudeAgentOptions(
            allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep", "WebFetch"],
            include_partial_messages=True,
        )

        if system:
            opts.system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": system,
            }
        if model is not None:
            opts.model = model.model_id

        # cwd is *always* required — it's the lookup key for Claude Code's
        # on-disk session store (~/.claude/projects/<encoded-cwd>/<id>.jsonl).
        # Without it, ``resume`` fails with "No conversation found", and a
        # fresh session lands under the wrong project dir.
        #
        # directory may be None when attach() creates a wrapper for a session
        # that existed before this process started (reconnect, server restart).
        # Recover it from the on-disk session file via the SDK before proceeding.
        if self.directory is None and self.id:
            info = await asyncio.to_thread(get_session_info, self.id)
            if info is not None:
                self.directory = getattr(info, "cwd", None)
        if self.directory is not None:
            opts.cwd = self.directory
        # Resume an existing session if we know the SDK-assigned UUID.
        # For pending new sessions both are empty — the SDK creates a fresh
        # session and we capture the id from the first response event.
        # For cold-attached sessions id == _sdk_id, so either works.
        if self._sdk_id:
            opts.resume = self._sdk_id
        elif self.id:
            opts.resume = self.id

        async def run_query():
            try:
                async for msg in query(prompt=text, options=opts):
                    await self._handle_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.exception("ClaudeCodeSession: query failed")
                await self._fan_out_error(str(err) or type(err).__name__)
                await self._fan_out_state(AgentState.IDLE)

        self._query_task = asyncio.create_task(run_query())
        await self._fan_out_state(AgentState.THINKING)

    async def cancel(self) -> None:
        # No-op when there's nothing to cancel. Lets aclose() iterate over
        # cached idle sessions cheaply, and makes user-triggered cancel safe
        # to call from anywhere without needing to know if a turn's running.
        if self._query_task is None or self._query_task.done():
            return
        self._cancelled.set()
        self._query_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._query_task
        await self._fan_out_state(AgentState.IDLE)
        self._text_accumulated = ""

    async def _handle_message(self, msg: Any) -> None:
        self._capture_session_id(msg)
        if isinstance(msg, SystemMessage):
            await self._handle_system(msg)
        elif isinstance(msg, AssistantMessage):
            await self._handle_assistant(msg)
        elif isinstance(msg, StreamEvent):
            await self._handle_stream(msg)
        elif isinstance(msg, ResultMessage):
            await self._handle_result(msg)

    def _capture_session_id(self, msg: Any) -> None:
        if self._sdk_id:
            return
        sid = getattr(msg, "session_id", None)
        if sid:
            self._assign_sdk_id(sid)

    async def _handle_system(self, msg: SystemMessage) -> None:
        if msg.subtype == "init":
            sid = msg.data.get("session_id")
            if sid and not self._sdk_id:
                self._assign_sdk_id(sid)

    def _assign_sdk_id(self, sdk_id: str) -> None:
        self._sdk_id = sdk_id
        if not self.id:
            self.id = sdk_id
        if self._on_sdk_id_assigned is not None:
            task = asyncio.ensure_future(self._on_sdk_id_assigned(sdk_id))
            self._sdk_assignment_tasks.add(task)
            task.add_done_callback(self._sdk_assignment_tasks.discard)

    async def _handle_assistant(self, msg: AssistantMessage) -> None:
        for block in msg.content:
            if not isinstance(block, ToolUseBlock):
                continue
            if block.name in self._announced_tools:
                continue
            self._announced_tools.add(block.name)
            for handler in tuple(self._tool_start_handlers):
                await handler(block.name, block.input)

    async def _handle_stream(self, msg: StreamEvent) -> None:
        event_type = msg.event.get("type", "")
        if event_type == "content_block_delta":
            delta_type = msg.event.get("delta", {}).get("type", "")
            if delta_type == "text_delta":
                delta = msg.event.get("delta", {}).get("text", "")
                self._text_accumulated += delta
                for handler in tuple(self._delta_handlers):
                    await handler(delta)

    async def _handle_result(self, msg: ResultMessage) -> None:
        text = msg.result or ""
        for handler in tuple(self._final_handlers):
            await handler(text)
        await self._fan_out_state(AgentState.IDLE)

    async def _fan_out_state(self, state: AgentState) -> None:
        self.current_state = state
        for handler in tuple(self._state_handlers):
            await handler(state)

    async def _fan_out_error(self, message: str) -> None:
        for handler in tuple(self._error_handlers):
            await handler(message)


# Static model catalog. Claude Code's SDK doesn't expose a runtime model
# listing, and the server-side defaults move when Anthropic ships new models —
# bumping this constant is the right place to track that. Names match the
# strings the SDK records on assistant messages (msg.message["model"]).
_CLAUDE_MODELS: list[ModelInfo] = [
    ModelInfo(
        provider_id="anthropic",
        provider_name="Anthropic",
        model_id="claude-opus-4-7",
        model_name="Claude Opus 4.7",
    ),
    ModelInfo(
        provider_id="anthropic",
        provider_name="Anthropic",
        model_id="claude-sonnet-4-6",
        model_name="Claude Sonnet 4.6",
    ),
    ModelInfo(
        provider_id="anthropic",
        provider_name="Anthropic",
        model_id="claude-haiku-4-5",
        model_name="Claude Haiku 4.5",
    ),
]
_CLAUDE_DEFAULT_MODEL = ModelChoice(provider_id="anthropic", model_id="claude-sonnet-4-6")


class ClaudeCodeProvider:
    """Provider implementation wrapping Claude Agent SDK.

    Persistence is backed by Claude Code's on-disk JSONL store at
    ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``. The SDK's
    sync helpers (``list_sessions`` / ``get_session_info`` /
    ``get_session_messages``) handle reads; we wrap them in
    ``asyncio.to_thread`` so the async API surface stays clean."""

    def __init__(self) -> None:
        # Cache live sessions so repeat ``attach`` returns the same instance —
        # matches OpencodeProvider semantics so observers can compose.
        self._sessions: dict[str, ClaudeCodeSession] = {}

    @property
    def provider_id(self) -> str:
        return "claude-code"

    # ── Live sessions ──────────────────────────────────────────────────

    async def create_session(
        self,
        title: str | None = None,
        *,
        directory: str | None = None,
    ) -> ClaudeCodeSession:
        # No real "create" call — Claude Code spawns the session id on the
        # first SDK query. We materialize a wrapper now and let the id land
        # when send_turn fires its first message.
        return ClaudeCodeSession(_http=None, title=title, directory=directory)

    def attach(self, session_id: str) -> ClaudeCodeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        # Cold-attach: session_id IS the SDK UUID.
        session = ClaudeCodeSession(_http=None, id=session_id, _sdk_id=session_id)
        self._sessions[session_id] = session
        return session

    def register_session_by_sdk_id(self, session: ClaudeCodeSession, sdk_id: str) -> None:
        """Index a pending session by its SDK-assigned UUID after the first turn fires."""
        self._sessions[sdk_id] = session

    # ── Persistence ────────────────────────────────────────────────────

    async def list_sessions(self, *, directory: str | None = None) -> list[SessionInfo]:
        rows = await asyncio.to_thread(list_sessions, directory=directory)
        return [_session_info_from_sdk(row) for row in rows]

    async def get_session(self, session_id: str) -> SessionInfo:
        session = self._sessions.get(session_id)
        if session is not None:
            sdk_id = session.sdk_id or session.id
            if not sdk_id:
                # Pending session — hasn't had a turn yet; return an in-memory stub.
                now = datetime.now(UTC)
                return SessionInfo(
                    id=session_id,
                    title=session.title or "",
                    directory=session.directory or "",
                    created_at=now,
                    updated_at=now,
                )
            info = await asyncio.to_thread(get_session_info, sdk_id)
            if info is None:
                raise SessionNotFound(f"claude-code session not found: {session_id}")
            result = _session_info_from_sdk(info)
            # Map the SDK UUID back to the caller's session_id when they differ.
            return dataclasses.replace(result, id=session_id) if session_id != sdk_id else result
        # Fallback: session_id is the SDK UUID (not in the live cache).
        info = await asyncio.to_thread(get_session_info, session_id)
        if info is None:
            raise SessionNotFound(f"claude-code session not found: {session_id}")
        return _session_info_from_sdk(info)

    async def get_transcript(self, session_id: str) -> list[Message]:
        # SDK returns parsed, sidechain-filtered messages but strips
        # per-message timestamps. We don't re-parse the raw JSONL to recover
        # them — completed_at stays None, which the UI already handles.
        session = self._sessions.get(session_id)
        if session is not None:
            sdk_id = session.sdk_id or session.id
            if not sdk_id:
                return []  # Pending session — no messages yet.
        else:
            sdk_id = session_id
        sdk_msgs = await asyncio.to_thread(get_session_messages, sdk_id)
        return [_message_from_sdk(m) for m in sdk_msgs]

    async def list_models(self) -> ModelCatalog:
        return ModelCatalog(models=list(_CLAUDE_MODELS), default=_CLAUDE_DEFAULT_MODEL)

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def aclose(self) -> None:
        # Cancel any in-flight live sessions so background query() tasks
        # stop pushing events into torn-down handlers.
        for session in list(self._sessions.values()):
            await session.cancel()
        self._sessions.clear()


def _session_info_from_sdk(row: object) -> SessionInfo:
    """SDKSessionInfo → our domain SessionInfo."""
    return SessionInfo(
        id=getattr(row, "session_id", ""),
        # custom_title takes precedence; fall back to the auto summary.
        title=getattr(row, "custom_title", None) or getattr(row, "summary", "") or "",
        directory=getattr(row, "cwd", None) or "",
        created_at=_ms_to_datetime(getattr(row, "created_at", None) or 0),
        updated_at=_ms_to_datetime(getattr(row, "last_modified", None) or 0),
    )


def _message_from_sdk(msg: object) -> Message:
    """SessionMessage → our domain Message.

    The SDK stores the raw Anthropic API message at ``msg.message`` (a dict
    with ``content``, ``model``, ``role``, …). We flatten the content
    blocks for ``text`` and ``parts``, and lift ``model`` onto our
    ModelChoice for assistant turns."""
    role = getattr(msg, "type", "")
    raw = getattr(msg, "message", None) or {}
    content = raw.get("content") if isinstance(raw, dict) else None
    parts: list[dict[str, object]] = []
    text_chunks: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            parts.append(block)
            if block.get("type") == "text":
                text_chunks.append(str(block.get("text", "")))
    elif isinstance(content, str):
        # User messages can be a bare string in the JSONL.
        text_chunks.append(content)
        parts.append({"type": "text", "text": content})
    model: ModelChoice | None = None
    if role == "assistant" and isinstance(raw, dict):
        model_id = raw.get("model")
        if isinstance(model_id, str):
            model = ModelChoice(provider_id="anthropic", model_id=model_id)
    return Message(
        role=role,
        text="".join(text_chunks),
        completed_at=None,
        parts=parts,
        model=model,
    )


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
