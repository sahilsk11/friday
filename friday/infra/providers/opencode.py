"""Opencode provider — implements the Provider/ProviderSession protocols."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Self

import httpx
from httpx_sse import EventSource, aconnect_sse

from friday.domain.provider import (
    ErrorHandler,
    Message,
    ModelCatalog,
    ModelChoice,
    ModelInfo,
    ReasoningHandler,
    SessionIdHandler,
    SessionInfo,
    StateHandler,
    TextDeltaHandler,
    TextFinalHandler,
    ToolStartHandler,
    Unsubscribe,
    subscribe,
)
from friday.domain.state import AgentState
from friday.infra.providers.events import (
    MessagePartDelta,
    MessagePartUpdated,
    MessageUpdated,
    OpencodeEvent,
    SessionError,
    SessionIdle,
    SessionStatus,
    parse_event,
)

logger = logging.getLogger("friday.opencode_provider")

EventHandler = Callable[[OpencodeEvent], Awaitable[None]]


class OpencodeProvider:
    """Owns the HTTP client and the single global SSE subscription."""

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
        body: dict[str, Any] = {"title": title} if title else {}
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
                logger.warning("SSE loop error — reconnecting | err=%s gen=%s", err, generation)
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
                logger.debug("non-JSON SSE data, skipping | data=%s", sse.data[:120])
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
        self._reasoning_handlers: list[ReasoningHandler] = []
        self._state_handlers: list[StateHandler] = []
        self._tool_start_handlers: list[ToolStartHandler] = []
        self._error_handlers: list[ErrorHandler] = []
        self._accumulated: dict[str, str] = {}
        self._completed: set[str] = set()
        self._announced_tools: set[str] = set()
        self._reasoning_parts: set[str] = set()
        self._announced_reasoning_parts: set[str] = set()
        self._current_state: AgentState = AgentState.IDLE

    # ── Observer registration ───────────────────────────────────────────────

    def on_text_delta(self, handler: TextDeltaHandler) -> Unsubscribe:
        return subscribe(self._delta_handlers, handler)

    def on_text_final(self, handler: TextFinalHandler) -> Unsubscribe:
        return subscribe(self._final_handlers, handler)

    def on_reasoning(self, handler: ReasoningHandler) -> Unsubscribe:
        return subscribe(self._reasoning_handlers, handler)

    def on_session_id(self, handler: SessionIdHandler) -> Unsubscribe:
        _ = handler
        return lambda: None

    def on_state(self, handler: StateHandler) -> Unsubscribe:
        return subscribe(self._state_handlers, handler)

    def on_tool_start(self, handler: ToolStartHandler) -> Unsubscribe:
        return subscribe(self._tool_start_handlers, handler)

    def on_error(self, handler: ErrorHandler) -> Unsubscribe:
        return subscribe(self._error_handlers, handler)

    @property
    def current_state(self) -> AgentState:
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
        self._accumulated.clear()
        self._reasoning_parts.clear()
        self._announced_reasoning_parts.clear()

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
            if (
                event.text
                and event.text.strip()
                and event.part_id not in self._announced_reasoning_parts
            ):
                self._announced_reasoning_parts.add(event.part_id)
                for reasoning_handler in tuple(self._reasoning_handlers):
                    await reasoning_handler(event.text)
            return
        if event.part_type == "text" and event.text and event.text.strip():
            key = f"{event.session_id}:{event.message_id}"
            if key not in self._accumulated:
                self._accumulated[key] = event.text
                for delta_handler in tuple(self._delta_handlers):
                    await delta_handler(event.text)
            return
        if event.part_type != "tool" or not event.tool_name:
            return
        if event.tool_status not in {"running", "completed"}:
            return
        if event.part_id in self._announced_tools:
            return
        self._announced_tools.add(event.part_id)
        for tool_handler in tuple(self._tool_start_handlers):
            await tool_handler(event.tool_name, event.tool_input)

    async def _handle_message_updated(self, event: MessageUpdated) -> None:
        if event.role != "assistant" or event.time_end is None:
            return
        key = f"{event.session_id}:{event.message_id}"
        if key in self._completed:
            return
        self._completed.add(key)
        text = self._accumulated.pop(key, "").strip()
        if text:
            for handler in tuple(self._final_handlers):
                await handler(text)
        await self._fan_out_state(AgentState.IDLE)

    async def _fan_out_state(self, state: AgentState) -> None:
        self._current_state = state
        for handler in tuple(self._state_handlers):
            await handler(state)

    async def _handle_error(self, event: SessionError) -> None:
        self._accumulated.clear()
        self._reasoning_parts.clear()
        self._announced_reasoning_parts.clear()
        for handler in tuple(self._error_handlers):
            await handler(event.message)
        await self._fan_out_state(AgentState.IDLE)


def _state_from_status(status: str) -> AgentState:
    return AgentState.THINKING if status == "busy" else AgentState.IDLE


# ── Wire shape parsers ──────────────────────────────────────────────────────


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
    text = _message_text(parts)
    time = info.get("time") or {}
    completed_ms = time.get("completed") or time.get("end")
    model_info = info.get("model") or {}
    model_id = info.get("modelID") or model_info.get("modelID")
    provider_id = info.get("providerID") or model_info.get("providerID")
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


def _message_text(parts: list[dict[str, Any]]) -> str:
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    if text:
        return text

    summaries: list[str] = []
    for part in parts:
        if part.get("type") != "tool":
            continue
        state = part.get("state") or {}
        tool = str(part.get("tool") or "tool")
        title = state.get("title")
        status = state.get("status")
        output = state.get("output")
        summary = str(title or tool)
        if status:
            summary = f"{summary} ({status})"
        if output and output != "(no output)":
            summary = f"{summary}: {output}"
        summaries.append(summary)
    return "\n".join(summaries)


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
