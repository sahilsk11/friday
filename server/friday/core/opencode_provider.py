"""Opencode provider — implements the Provider/ProviderSession protocols.

Application code talks to this only via ``friday.core.provider``. The
HTTP+SSE machinery, the per-session event dispatch, the reconnect policy —
all of that is internal to this module.

Ported from ``~/Projects/friday/backend/src/agent/opencodeAdapter.ts``.

Key invariants:
- One global SSE subscription per ``OpencodeProvider``; events are routed
  to the right :class:`OpencodeSession` by ``sessionID``. The shared
  connection is the reason provider+session live in one file: splitting
  them would only obscure the multiplexing.
- ``message.updated`` for ``role == "assistant"`` with ``time.end`` set is
  the signal that fires ``on_text_final`` and ``on_state(IDLE)``. Without
  it, queued turns stall (friday v1 incident).
- Reconnect with exponential backoff (capped at 5s); a generation counter
  lets stale loops bail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Self

import httpx
from httpx_sse import EventSource, aconnect_sse
from loguru import logger

from friday.core.events import (
    MessagePartDelta,
    MessagePartUpdated,
    MessageUpdated,
    OpencodeEvent,
    SessionError,
    SessionIdle,
    SessionStatus,
    parse_event,
)
from friday.core.provider import (
    ErrorHandler,
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

# Sent on every prompt_async via the per-turn ``system`` field (the only
# mechanism opencode actually honors — its create-time systemPrompt is silently
# dropped, see scripts/probe_systemprompt_variants.py).
SYSTEM_PROMPT_VOICE = (
    "You are speaking out loud through TTS — the user hears you as audio, not "
    "text on a screen.\n"
    "Use plain prose. No markdown of any kind: no asterisks, backticks, "
    "bullets, numbered lists, headers, or code fences.\n"
    'Say file names, routes, and commands the way a person speaks aloud, not '
    'the way they\'re written. "VoiceRoom.tsx" → "the voice room component." '
    '"/api/sessions" → "the sessions endpoint." "npm run dev" → "the dev '
    'server." Drop slashes, file extensions, and underscores.\n\n'
    "Keep answers concise and biased towards the next action required. The "
    "user wants only relevant information to the prompt spoken — never say "
    'stuff like "got it, here\'s your voice friendly response" — just jump '
    "into the point.\n\n"
    "Before you start work that takes a moment — exploring the codebase, "
    "running a build, gathering info — say one short sentence about what "
    "you're about to do. When you're done, say one short sentence about what "
    "you found. Don't narrate each individual tool call: if you read fifteen "
    "files to answer one question, that's one sentence before and one "
    "sentence after, not fifteen. The user can't see your tool calls, so "
    "your voice is their only signal that work is happening."
)

EventHandler = Callable[[OpencodeEvent], Awaitable[None]]


class OpencodeProvider:
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
    def provider_id(self) -> str:
        """Provider identifier — opencode."""
        return "opencode"

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

    # ── Live sessions ──────────────────────────────────────────────────

    async def create_session(
        self,
        title: str | None = None,
        *,
        directory: str | None = None,
    ) -> OpencodeSession:
        # POST /session body only accepts {parentID, title} per the opencode
        # server docs. There's no create-time system prompt — that goes per-turn
        # via send_turn(system=...).
        body: dict[str, Any] = {"title": title} if title else {}
        # Opencode takes the working directory as a query param (see the
        # SDK's SessionCreateData.query.directory). Without it, opencode
        # defaults to whichever cwd the `opencode serve` process was
        # launched from — usually wrong for our use case.
        params: dict[str, str] = {"directory": directory} if directory else {}
        resp = await self._http.post("/session", json=body, params=params)
        resp.raise_for_status()
        session_id: str = resp.json()["id"]
        return self.attach(session_id)

    def attach(self, session_id: str) -> OpencodeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = OpencodeSession(self._http, session_id)
        self._sessions[session_id] = session
        return session

    # ── Persistence ────────────────────────────────────────────────────

    async def list_sessions(self, *, directory: str | None = None) -> list[SessionInfo]:
        resp = await self._http.get("/experimental/session")
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json()
        sessions = [_parse_session_info(row) for row in rows]
        if directory is not None:
            sessions = [s for s in sessions if s.directory == directory]
        return sessions

    async def get_session(self, session_id: str) -> SessionInfo:
        resp = await self._http.get(f"/session/{session_id}")
        resp.raise_for_status()
        return _parse_session_info(resp.json())

    async def get_transcript(self, session_id: str) -> list[Message]:
        resp = await self._http.get(f"/session/{session_id}/message")
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json()
        return [_parse_message(row) for row in rows]

    async def list_models(self) -> ModelCatalog:
        """Proxy ``GET /config/providers`` filtered to active + toolcall-capable
        models. ``default`` is whichever provider opencode surfaces first in
        its global default map."""
        resp = await self._http.get("/config/providers")
        resp.raise_for_status()
        payload = resp.json()
        providers = payload.get("providers") or []
        models: list[ModelInfo] = []
        for prov in providers:
            provider_id = prov.get("id", "")
            provider_name = prov.get("name", provider_id)
            for model_id, model in (prov.get("models") or {}).items():
                caps = model.get("capabilities") or {}
                if model.get("status") != "active":
                    continue
                if not caps.get("toolcall"):
                    continue
                models.append(
                    ModelInfo(
                        provider_id=provider_id,
                        provider_name=provider_name,
                        model_id=model_id,
                        model_name=model.get("name", model_id),
                    )
                )
        default_map: dict[str, str] = payload.get("default") or {}
        default: ModelChoice | None = None
        for provider_id, model_id in default_map.items():
            default = ModelChoice(provider_id=provider_id, model_id=model_id)
            break
        return ModelCatalog(models=models, default=default)

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
        self._error_handlers: list[ErrorHandler] = []
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
        # Latest agent state. Updated on every fan-out so reconnecting
        # consumers (a fresh ProviderSessionProcessor, a REST GET) can read the
        # current value without waiting for the next opencode transition.
        self._current_state: AgentState = AgentState.IDLE

    # ── Observer registration ───────────────────────────────────────────────
    #
    # Each subscriber gets back an ``Unsubscribe`` function. Pipelines must
    # call it on teardown — otherwise dead handlers accumulate on the cached
    # session and push frames into pipelines that no longer exist.

    def on_text_delta(self, handler: TextDeltaHandler) -> Unsubscribe:
        return subscribe(self._delta_handlers, handler)

    def on_text_final(self, handler: TextFinalHandler) -> Unsubscribe:
        return subscribe(self._final_handlers, handler)

    def on_state(self, handler: StateHandler) -> Unsubscribe:
        return subscribe(self._state_handlers, handler)

    def on_tool_start(self, handler: ToolStartHandler) -> Unsubscribe:
        """Fires once per tool invocation, with the tool name."""
        return subscribe(self._tool_start_handlers, handler)

    def on_error(self, handler: ErrorHandler) -> Unsubscribe:
        """Fires when the session encounters an error (e.g., API failure)."""
        return subscribe(self._error_handlers, handler)

    @property
    def current_state(self) -> AgentState:
        """Latest agent state seen on the SSE stream. Defaults to IDLE."""
        return self._current_state

    # ── Outbound ────────────────────────────────────────────────────────────

    async def send_turn(
        self,
        text: str,
        model: ModelChoice | None = None,
        *,
        system: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model is not None:
            body["model"] = {"providerID": model.provider_id, "modelID": model.model_id}
        if system is not None:
            body["system"] = system
        resp = await self._http.post(f"/session/{self.id}/prompt_async", json=body)
        resp.raise_for_status()

    async def cancel(self) -> None:
        resp = await self._http.post(f"/session/{self.id}/abort", json={})
        resp.raise_for_status()
        # Drop in-flight accumulators so a late MessageUpdated for the aborted
        # turn doesn't fire on_text_final / on_state(IDLE) into a fresh turn.
        self._accumulated.clear()
        self._reasoning_parts.clear()

    # ── Inbound (called by OpencodeProvider._dispatch) ────────────────────────

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
        elif isinstance(event, SessionError):
            await self._handle_error(event)

    async def _handle_delta(self, event: MessagePartDelta) -> None:
        if event.field != "text":
            return
        if event.part_id in self._reasoning_parts:
            return
        key = f"{event.session_id}:{event.message_id}"
        self._accumulated[key] = self._accumulated.get(key, "") + event.delta
        for handler in tuple(self._delta_handlers):
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
        for handler in tuple(self._tool_start_handlers):
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
            for handler in tuple(self._final_handlers):
                await handler(text)
        await self._fan_out_state(AgentState.IDLE)

    async def _fan_out_state(self, state: AgentState) -> None:
        # Opencode 1.14 emits the same terminal state several times in quick
        # succession (``session.status:idle`` + ``session.idle`` + a stray
        # busy→idle flip). Consumers must be idempotent on repeat states —
        # don't restart TTS or replay UI transitions on a duplicate.
        self._current_state = state
        # Snapshot to a tuple so a handler that unsubscribes itself mid-loop
        # doesn't mutate the list we're iterating.
        for handler in tuple(self._state_handlers):
            await handler(state)

    async def _handle_error(self, event: SessionError) -> None:
        self._accumulated.clear()
        self._reasoning_parts.clear()
        for handler in tuple(self._error_handlers):
            await handler(event.message)
        await self._fan_out_state(AgentState.IDLE)


def _state_from_status(status: str) -> AgentState:
    return AgentState.THINKING if status == "busy" else AgentState.IDLE


# ── Wire shape parsers ──────────────────────────────────────────────────────
#
# Pinned by ``scripts/probe_session_manager.py`` against a real opencode 1.14
# server; if opencode bumps the schema, the probe is the canary.


def _parse_session_info(row: dict[str, Any]) -> SessionInfo:
    time = row.get("time") or {}
    return SessionInfo(
        id=row["id"],
        title=row.get("title", ""),
        directory=row.get("directory", ""),
        created_at=_ms_to_datetime(time.get("created", 0)),
        updated_at=_ms_to_datetime(time.get("updated", time.get("created", 0))),
    )


def _parse_message(row: dict[str, Any]) -> Message:
    info = row.get("info") or {}
    parts: list[dict[str, Any]] = row.get("parts") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    time = info.get("time") or {}
    completed_ms = time.get("completed") or time.get("end")
    model_id = info.get("modelID")
    provider_id = info.get("providerID")
    model = (
        ModelChoice(provider_id=provider_id, model_id=model_id)
        if model_id and provider_id
        else None
    )
    error_info = info.get("error") or {}
    error_msg = error_info.get("data", {}).get("message") if error_info else None
    return Message(
        role=info.get("role", ""),
        text=text,
        completed_at=_ms_to_datetime(completed_ms) if completed_ms else None,
        parts=parts,
        model=model,
        error=error_msg,
    )


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
