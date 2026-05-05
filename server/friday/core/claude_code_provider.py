"""ClaudeCode provider using Anthropic Agent SDK.

Wraps the Claude Agent SDK library to expose the same Provider + ProviderSession
interface as OpenCode.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

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
    project_key_for_directory,
    query,
)
from loguru import logger

from friday.core.provider import (
    Message,
    ModelCatalog,
    ModelChoice,
    ModelInfo,
    SessionInfo,
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

    id: str = ""
    title: str | None = None
    directory: str | None = None
    current_state: AgentState = AgentState.IDLE

    _delta_handlers: list[TextDeltaHandler] = field(default_factory=list, repr=False)
    _final_handlers: list[TextFinalHandler] = field(default_factory=list, repr=False)
    _state_handlers: list[StateHandler] = field(default_factory=list, repr=False)
    _tool_start_handlers: list[ToolStartHandler] = field(default_factory=list, repr=False)

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
        if self.directory is not None:
            opts.cwd = self.directory

        async def run_query():
            async for msg in query(prompt=text, options=opts):
                await self._handle_message(msg)

        self._query_task = asyncio.create_task(run_query())
        await self._fan_out_state(AgentState.THINKING)

    async def cancel(self) -> None:
        self._cancelled.set()
        if self._query_task is not None:
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
        if self.id:
            return
        sid = getattr(msg, "session_id", None)
        if sid:
            self.id = sid

    async def _handle_system(self, msg: SystemMessage) -> None:
        if msg.subtype == "init":
            sid = msg.data.get("session_id")
            if sid:
                self.id = sid

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
        session = ClaudeCodeSession(_http=None, title=title, directory=directory)
        return session

    def attach(self, session_id: str) -> ClaudeCodeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = ClaudeCodeSession(_http=None, id=session_id)
        self._sessions[session_id] = session
        return session

    # ── Persistence ────────────────────────────────────────────────────

    async def list_sessions(self, *, directory: str | None = None) -> list[SessionInfo]:
        rows = await asyncio.to_thread(list_sessions, directory=directory)
        return [_session_info_from_sdk(row) for row in rows]

    async def get_session(self, session_id: str) -> SessionInfo:
        info = await asyncio.to_thread(get_session_info, session_id)
        if info is None:
            raise LookupError(f"claude-code session not found: {session_id}")
        return _session_info_from_sdk(info)

    async def get_transcript(self, session_id: str) -> list[Message]:
        # Two reads: SDK gives us message structure (parsed, sidechain
        # filtered); the raw JSONL gives us per-message timestamps the SDK
        # strips. Join on uuid.
        sdk_msgs, ts_by_uuid = await asyncio.to_thread(
            _read_transcript_with_timestamps, session_id
        )
        return [_message_from_sdk(m, ts_by_uuid) for m in sdk_msgs]

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


def _read_transcript_with_timestamps(
    session_id: str,
) -> tuple[Sequence[object], dict[str, datetime]]:
    """Synchronous helper that pulls SDK messages and the matching JSONL
    timestamps. Returned untyped (the SDK doesn't export ``SessionMessage``
    as a public stable type) — the caller projects them into ``Message``."""
    info = get_session_info(session_id)
    if info is None:
        raise LookupError(f"claude-code session not found: {session_id}")
    sdk_msgs = get_session_messages(session_id)
    ts_by_uuid: dict[str, datetime] = {}
    cwd = getattr(info, "cwd", None)
    if cwd:
        jsonl_path = Path.home() / ".claude" / "projects" / project_key_for_directory(cwd) / (
            f"{session_id}.jsonl"
        )
        if jsonl_path.is_file():
            with jsonl_path.open() as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    uid = entry.get("uuid")
                    ts = entry.get("timestamp")
                    if uid and ts:
                        try:
                            ts_by_uuid[uid] = datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            )
                        except ValueError:
                            continue
    return sdk_msgs, ts_by_uuid


def _message_from_sdk(msg: object, ts_by_uuid: dict[str, datetime]) -> Message:
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
        completed_at=ts_by_uuid.get(getattr(msg, "uuid", "")),
        parts=parts,
        model=model,
    )


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
