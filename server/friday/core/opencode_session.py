"""OpencodeClient + OpencodeSession — HTTP + SSE wrapper for opencode.

Ported from ``~/Projects/friday/backend/src/agent/opencodeAdapter.ts``.

Key invariants:
- One global SSE subscription per ``OpencodeClient``; events are routed to the
  right :class:`OpencodeSession` by ``sessionID``.
- ``message.updated`` for ``role == "assistant"`` with ``time.end`` set is the
  signal that fires ``on_text_final`` and ``on_state(IDLE)``. Without it,
  queued turns stall (friday v1 incident).
- Reconnect with exponential backoff (capped at 5s); a generation counter
  lets stale loops bail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Self

import httpx
from httpx_sse import EventSource, aconnect_sse
from loguru import logger

from friday.core.events import (
    MessagePartDelta,
    MessagePartUpdated,
    MessageUpdated,
    OpencodeEvent,
    SessionIdle,
    SessionStatus,
    parse_event,
)
from friday.core.state import AgentState

SYSTEM_PROMPT_VOICE = (
    "You are being used via a voice interface with TTS (text-to-speech). "
    "Keep responses concise and natural for speech. "
    "Avoid formatting like markdown, code blocks, or long lists when possible. "
    "Use short paragraphs and speak in a conversational tone."
)

EventHandler = Callable[[OpencodeEvent], Awaitable[None]]
TextDeltaHandler = Callable[[str], Awaitable[None]]
TextFinalHandler = Callable[[str], Awaitable[None]]
StateHandler = Callable[[AgentState], Awaitable[None]]
ToolStartHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class OpencodeClient:
    """Owns the HTTP client and the single global SSE subscription.

    Sessions are created via :meth:`new_session` or :meth:`session` (existing).
    """

    def __init__(self, base_url: str, *, reconnect_max_delay: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)
        self._sessions: dict[str, OpencodeSession] = {}
        self._sse_task: asyncio.Task[None] | None = None
        self._sse_generation = 0
        self._reconnect_max_delay = reconnect_max_delay
        self._connected = asyncio.Event()
        self._closed = False

    @property
    def http(self) -> httpx.AsyncClient:
        """Shared HTTP client. SessionManager and other layers use this for typed calls."""
        return self._http

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Begin the SSE loop. Returns once the first event has been received."""
        if self._sse_task is not None:
            return
        self._sse_task = asyncio.create_task(self._run_sse_loop(), name="opencode-sse")
        await self._connected.wait()

    async def aclose(self) -> None:
        self._closed = True
        if self._sse_task is not None:
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sse_task
            self._sse_task = None
        await self._http.aclose()

    # ── HTTP API ────────────────────────────────────────────────────────────

    async def new_session(
        self,
        title: str | None = None,
        system_prompt: str | None = None,
        *,
        directory: str | None = None,
    ) -> OpencodeSession:
        body: dict[str, Any] = {"title": title} if title else {}
        if system_prompt:
            body["systemPrompt"] = system_prompt
        # Opencode takes the working directory as a query param (see the
        # SDK's SessionCreateData.query.directory). Without it, opencode
        # defaults to whichever cwd the `opencode serve` process was
        # launched from — usually wrong for our use case.
        params: dict[str, str] = {"directory": directory} if directory else {}
        resp = await self._http.post("/session", json=body, params=params)
        resp.raise_for_status()
        session_id: str = resp.json()["id"]
        return self.session(session_id)

    def session(self, session_id: str) -> OpencodeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = OpencodeSession(self._http, session_id)
        self._sessions[session_id] = session
        return session

    async def list_sessions(self) -> list[dict[str, Any]]:
        resp = await self._http.get("/session")
        resp.raise_for_status()
        return resp.json()

    # ── SSE loop ────────────────────────────────────────────────────────────

    async def _run_sse_loop(self) -> None:
        attempts = 0
        while not self._closed:
            generation = self._sse_generation = self._sse_generation + 1
            try:
                async with aconnect_sse(self._http, "GET", "/global/event") as source:
                    attempts = 0
                    self._connected.set()
                    await self._consume_sse(source, generation)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.warning("SSE loop error — reconnecting | err={} gen={}", err, generation)
            if self._closed:
                return
            delay = min(self._reconnect_max_delay, 0.25 * (2**attempts))
            attempts += 1
            await asyncio.sleep(delay)

    async def _consume_sse(self, source: EventSource, generation: int) -> None:
        async for sse in source.aiter_sse():
            if generation != self._sse_generation:
                return
            if not sse.data:
                continue
            try:
                raw = json.loads(sse.data)
            except json.JSONDecodeError:
                logger.debug("non-JSON SSE data, skipping | data={}", sse.data[:120])
                continue
            event = parse_event(raw)
            if event is None:
                continue
            await self._dispatch(event)

    async def _dispatch(self, event: OpencodeEvent) -> None:
        session_id = getattr(event, "session_id", None)
        if session_id is None:
            return
        session = self._sessions.get(session_id)
        if session is None:
            return
        await session.dispatch(event)


class OpencodeSession:
    """One opencode session. Observers attach to receive text deltas + state."""

    def __init__(self, http: httpx.AsyncClient, session_id: str) -> None:
        self._http = http
        self.id = session_id
        self._delta_handlers: list[TextDeltaHandler] = []
        self._final_handlers: list[TextFinalHandler] = []
        self._state_handlers: list[StateHandler] = []
        self._tool_start_handlers: list[ToolStartHandler] = []
        # Accumulated text per (sessionID, messageID) for on_text_final.
        self._accumulated: dict[str, str] = {}
        # Track which assistant messages we've already finalized.
        self._completed: set[str] = set()
        # Track tool parts we've already announced to avoid replays — opencode
        # emits MessagePartUpdated repeatedly as tool state advances.
        self._announced_tools: set[str] = set()
        # ReasoningPart (thinking) deltas arrive as field="text" on the wire,
        # indistinguishable from real text deltas unless we track the part type.
        # We register part IDs here when we first see them as type="reasoning"
        # so _handle_delta can skip them entirely.
        self._reasoning_parts: set[str] = set()

    # ── Observer registration ───────────────────────────────────────────────

    def on_text_delta(self, handler: TextDeltaHandler) -> None:
        self._delta_handlers.append(handler)

    def on_text_final(self, handler: TextFinalHandler) -> None:
        self._final_handlers.append(handler)

    def on_state(self, handler: StateHandler) -> None:
        self._state_handlers.append(handler)

    def on_tool_start(self, handler: ToolStartHandler) -> None:
        """Fires once per tool invocation, with the tool name."""
        self._tool_start_handlers.append(handler)

    # ── Outbound ────────────────────────────────────────────────────────────

    async def send_turn(self, text: str) -> None:
        body = {"parts": [{"type": "text", "text": text}]}
        resp = await self._http.post(f"/session/{self.id}/prompt_async", json=body)
        resp.raise_for_status()

    async def cancel(self) -> None:
        resp = await self._http.post(f"/session/{self.id}/abort", json={})
        resp.raise_for_status()
        # Drop in-flight accumulators so a late MessageUpdated for the aborted
        # turn doesn't fire on_text_final / on_state(IDLE) into a fresh turn.
        self._accumulated.clear()
        self._reasoning_parts.clear()

    # ── Inbound (called by OpencodeClient._dispatch) ────────────────────────

    async def dispatch(self, event: OpencodeEvent) -> None:
        if isinstance(event, MessagePartDelta):
            await self._handle_delta(event)
        elif isinstance(event, MessageUpdated):
            await self._handle_message_updated(event)
        elif isinstance(event, MessagePartUpdated):
            await self._handle_part_updated(event)
        elif isinstance(event, SessionStatus):
            await self._fan_out_state(_state_from_status(event.status))
        elif isinstance(event, SessionIdle):
            await self._fan_out_state(AgentState.IDLE)

    async def _handle_delta(self, event: MessagePartDelta) -> None:
        if event.field != "text":
            return
        if event.part_id in self._reasoning_parts:
            return
        key = f"{event.session_id}:{event.message_id}"
        self._accumulated[key] = self._accumulated.get(key, "") + event.delta
        for handler in self._delta_handlers:
            await handler(event.delta)

    async def _handle_part_updated(self, event: MessagePartUpdated) -> None:
        if event.part_type == "reasoning":
            self._reasoning_parts.add(event.part_id)
            return

        # Announce on "running" (not "pending") — the input args aren't
        # populated until the tool actually starts executing.
        if event.part_type != "tool" or not event.tool_name:
            return
        if event.tool_status != "running":
            return
        if event.part_id in self._announced_tools:
            return
        self._announced_tools.add(event.part_id)
        for handler in self._tool_start_handlers:
            await handler(event.tool_name, event.tool_input)

    async def _handle_message_updated(self, event: MessageUpdated) -> None:
        if event.role != "assistant" or event.time_end is None:
            return
        key = f"{event.session_id}:{event.message_id}"
        if key in self._completed:
            return
        self._completed.add(key)
        text = self._accumulated.pop(key, "")
        if text:
            for handler in self._final_handlers:
                await handler(text)
        await self._fan_out_state(AgentState.IDLE)

    async def _fan_out_state(self, state: AgentState) -> None:
        # Opencode 1.14 emits the same terminal state several times in quick
        # succession (``session.status:idle`` + ``session.idle`` + a stray
        # busy→idle flip). Consumers must be idempotent on repeat states —
        # don't restart TTS or replay UI transitions on a duplicate.
        for handler in self._state_handlers:
            await handler(state)


def _state_from_status(status: str) -> AgentState:
    return AgentState.THINKING if status == "busy" else AgentState.IDLE
