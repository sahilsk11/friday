"""Codex provider — implements the Provider/ProviderSession protocols.

Wraps the Codex CLI (codex exec) to expose the same interface as OpenCode
and ClaudeCode providers.

Key invariants:
- Spawns ``codex exec --json`` as a subprocess for non-interactive execution.
- Parses JSONL events from stdout for streaming (text deltas, tool calls).
- Sessions are backed by Codex's on-disk session store
  (``~/.codex/sessions/<year>/<month>/<day>/``).
- Uses ``--cd`` to pin the working directory per session.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

_CODEX_SESSION_DIR = Path("/root/projects/.codex/sessions")


@dataclass
class CodexSession:
    """One Codex session wrapping the codex exec subprocess.

    Maps subprocess events to the ProviderSession observer API:
    - text_delta events → on_text_delta
    - tool_use events → on_tool_start
    - message_end / done events → on_text_final + state(IDLE)
    """

    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _cancelled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    id: str = ""
    title: str | None = None
    directory: str | None = None
    current_state: AgentState = AgentState.IDLE

    _thread_id: str | None = None
    _turn_id: str | None = None

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

    async def send_turn(
        self,
        text: str,
        model: ModelChoice | None = None,
        *,
        system: str | None = None,
    ) -> None:
        if self._task is not None and not self._task.done():
            logger.warning("CodexSession: already has a turn in-flight")
            return

        self._text_accumulated = ""
        self._announced_tools.clear()

        cmd = ["codex", "exec", "--json", "-s", "danger-full-access", "--skip-git-repo-check"]
        if self.directory:
            cmd.extend(["--cd", self.directory])
        if model is not None and model.model_id:
            cmd.extend(["--model", model.model_id])

        prompt = f"{system}\n\n{text}" if system else text

        logger.debug("codex exec cmd: {}", cmd)

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def run():
            if self._process is None or self._process.stdin is None:
                return
            self._process.stdin.write(prompt.encode())
            await self._process.stdin.drain()
            self._process.stdin.close()
            await self._process.wait()

        async def read_output():
            if self._process:
                await self._read_events(self._process.stdout)

        self._reader_task = asyncio.create_task(read_output())
        self._task = asyncio.create_task(run())
        await self._fan_out_state(AgentState.THINKING)

    async def cancel(self) -> None:
        if self._task is None or self._task.done():
            return
        self._cancelled.set()
        self._task.cancel()
        if self._reader_task:
            self._reader_task.cancel()
        try:
            if self._task:
                await self._task
        except asyncio.CancelledError:
            pass
        if self._process:
            self._process.terminate()
            await self._process.wait()
        await self._fan_out_state(AgentState.IDLE)
        self._text_accumulated = ""

    async def _read_events(self, stdout: asyncio.StreamReader | None) -> None:
        if stdout is None:
            return
        while True:
            try:
                line = await asyncio.wait_for(stdout.readline(), timeout=60.0)
            except asyncio.CancelledError:
                break
            except TimeoutError:
                break
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self._handle_event(event)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")

        if event_type == "thread.started":
            self._thread_id = event.get("thread_id")
            self.id = self._thread_id or ""

        elif event_type == "turn.started":
            self._turn_id = event.get("turn_id")
            await self._fan_out_state(AgentState.THINKING)

        elif event_type == "turn.completed":
            self._turn_id = None
            if self._text_accumulated:
                for handler in tuple(self._final_handlers):
                    await handler(self._text_accumulated)
            await self._fan_out_state(AgentState.IDLE)

        elif event_type in ("error", "turn.failed"):
            message = event.get("message", "")
            if not message:
                error_data = event.get("error", {})
                if isinstance(error_data, dict):
                    message = error_data.get("message", "") or str(error_data)
            if message:
                await self._fan_out_error(message)
            await self._fan_out_state(AgentState.IDLE)

        elif event_type == "item.started":
            item = event.get("item", {})
            item_type = item.get("type", "")
            item_id = item.get("id", "")

            if item_type == "agent_message":
                pass

            elif item_type == "command_execution":
                command = item.get("command", "")
                for handler in tuple(self._tool_start_handlers):
                    await handler("bash", {"command": command, "item_id": item_id})

            elif item_type == "tool_use":
                tool_name = item.get("name", "")
                tool_input = event.get("input", {})
                if tool_name and tool_name not in self._announced_tools:
                    self._announced_tools.add(tool_name)
                    for handler in tuple(self._tool_start_handlers):
                        await handler(tool_name, tool_input)

        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")

            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    self._text_accumulated += text

            elif item_type == "command_execution":
                status = item.get("status", "")
                if status == "completed":
                    pass

            elif item_type == "tool_use":
                pass

    async def _fan_out_state(self, state: AgentState) -> None:
        self.current_state = state
        for handler in tuple(self._state_handlers):
            await handler(state)

    async def _fan_out_error(self, message: str) -> None:
        for handler in tuple(self._error_handlers):
            await handler(message)


_CODEX_MODELS: list[ModelInfo] = [
    ModelInfo(
        provider_id="openai",
        provider_name="OpenAI",
        model_id="gpt-5.5",
        model_name="GPT-5.5",
    ),
    ModelInfo(
        provider_id="openai",
        provider_name="OpenAI",
        model_id="gpt-5.4",
        model_name="GPT-5.4",
    ),
    ModelInfo(
        provider_id="openai",
        provider_name="OpenAI",
        model_id="gpt-5.4-mini",
        model_name="GPT-5.4 Mini",
    ),
    ModelInfo(
        provider_id="openai",
        provider_name="OpenAI",
        model_id="gpt-5.3-codex",
        model_name="GPT-5.3 Codex",
    ),
    ModelInfo(
        provider_id="openai",
        provider_name="OpenAI",
        model_id="gpt-oss-120b",
        model_name="GPT-Oss 120B",
    ),
    ModelInfo(
        provider_id="openai",
        provider_name="OpenAI",
        model_id="gpt-oss-20b",
        model_name="GPT-Oss 20B",
    ),
]
_CODEX_DEFAULT_MODEL = ModelChoice(provider_id="openai", model_id="gpt-5.5")


class CodexProvider:
    """Provider implementation wrapping the Codex CLI.

    Persistence is backed by Codex's on-disk JSONL store at
    ``~/.codex/sessions/<year>/<month>/<day>/<session-id>.jsonl``.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, CodexSession] = {}

    @property
    def provider_id(self) -> str:
        return "codex"

    # ── Live sessions ──────────────────────────────────────────────────

    async def create_session(
        self,
        title: str | None = None,
        *,
        directory: str | None = None,
    ) -> CodexSession:
        return CodexSession(title=title, directory=directory)

    def attach(self, session_id: str) -> CodexSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = CodexSession(id=session_id)
        self._sessions[session_id] = session
        return session

    # ── Persistence ────────────────────────────────────────────────────

    async def list_sessions(self, *, directory: str | None = None) -> list[SessionInfo]:
        if not _CODEX_SESSION_DIR.exists():
            return []
        sessions: list[SessionInfo] = []
        for year_dir in _CODEX_SESSION_DIR.iterdir():
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir():
                    continue
                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir():
                        continue
                    for session_file in day_dir.glob("*.jsonl"):
                        sid = session_file.stem.split("-")[-1]
                        sessions.append(
                            SessionInfo(
                                id=sid,
                                title=session_file.stem,
                                directory=str(day_dir),
                                created_at=datetime.fromtimestamp(
                                    session_file.stat().st_ctime, tz=UTC
                                ),
                                updated_at=datetime.fromtimestamp(
                                    session_file.stat().st_mtime, tz=UTC
                                ),
                            )
                        )
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)[:50]

    async def get_session(self, session_id: str) -> SessionInfo:
        for year_dir in _CODEX_SESSION_DIR.iterdir():
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir():
                    continue
                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir():
                        continue
                    matches = list(day_dir.glob(f"*{session_id}*.jsonl"))
                    if matches:
                        f = matches[0]
                        return SessionInfo(
                            id=session_id,
                            title=f.stem,
                            directory=str(day_dir),
                            created_at=datetime.fromtimestamp(f.stat().st_ctime, tz=UTC),
                            updated_at=datetime.fromtimestamp(f.stat().st_mtime, tz=UTC),
                        )
        raise SessionNotFound(f"codex session not found: {session_id}")

    async def get_transcript(self, session_id: str) -> list[Message]:
        messages = []
        for year_dir in _CODEX_SESSION_DIR.iterdir():
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir():
                    continue
                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir():
                        continue
                    matches = list(day_dir.glob(f"*{session_id}*.jsonl"))
                    if not matches:
                        continue
                    with open(matches[0]) as f:
                        for line in f:
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            role = record.get("role", "")
                            content = record.get("content", "") or []
                            text = ""
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        text += block.get("text", "")
                            elif isinstance(content, str):
                                text = content
                            messages.append(
                                Message(
                                    role=role,
                                    text=text,
                                    completed_at=datetime.fromtimestamp(
                                        record.get("timestamp", 0) / 1000, tz=UTC
                                    )
                                    if record.get("timestamp")
                                    else None,
                                )
                            )
        return messages

    async def list_models(self) -> ModelCatalog:
        return ModelCatalog(models=list(_CODEX_MODELS), default=_CODEX_DEFAULT_MODEL)

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def aclose(self) -> None:
        for session in list(self._sessions.values()):
            await session.cancel()
        self._sessions.clear()
