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
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
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

_CODEX_HOME = (
    Path(os.environ["CODEX_HOME"]) if "CODEX_HOME" in os.environ else Path.home() / ".codex"
)
_CODEX_SESSION_DIR = _CODEX_HOME / "sessions"


def _read_codex_session_id(session_file: Path) -> str | None:
    try:
        with session_file.open() as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload", {})
                if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                    return payload["id"]
    except OSError:
        return None
    return None


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
    _stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _sdk_assignment_tasks: set[asyncio.Future[None]] = field(default_factory=set, repr=False)
    _cancelled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    id: str = ""
    title: str | None = None
    directory: str | None = None
    current_state: AgentState = AgentState.IDLE

    _thread_id: str | None = None
    _turn_id: str | None = None
    _on_sdk_id_assigned: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False)

    _delta_handlers: list[TextDeltaHandler] = field(default_factory=list, repr=False)
    _final_handlers: list[TextFinalHandler] = field(default_factory=list, repr=False)
    _state_handlers: list[StateHandler] = field(default_factory=list, repr=False)
    _tool_start_handlers: list[ToolStartHandler] = field(default_factory=list, repr=False)
    _error_handlers: list[ErrorHandler] = field(default_factory=list, repr=False)

    _text_accumulated: str = ""
    _announced_tools: set[str] = field(default_factory=set, repr=False)

    def _logger(self, **extra: str) -> Any:
        session_id = self._thread_id or self.id or "-"
        turn_id = self._turn_id or "-"
        return logger.bind(session_id=session_id, turn_id=turn_id, **extra)

    def _assign_thread_id(self, thread_id: str) -> None:
        if self._thread_id == thread_id:
            return
        self._thread_id = thread_id
        if not self.id:
            self.id = thread_id
        if self._on_sdk_id_assigned is not None:
            task = asyncio.ensure_future(self._on_sdk_id_assigned(thread_id))
            self._sdk_assignment_tasks.add(task)
            task.add_done_callback(self._sdk_assignment_tasks.discard)

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
            self._logger().warning("codex: dropped turn because another turn is in-flight")
            return

        self._text_accumulated = ""
        self._announced_tools.clear()

        cmd = ["codex", "exec", "--json", "-s", "danger-full-access", "--skip-git-repo-check"]
        if self.directory:
            cmd.extend(["--cd", self.directory])
        if model is not None and model.model_id:
            cmd.extend(["--model", model.model_id])

        prompt = f"{system}\n\n{text}" if system else text

        self._logger().info(
            "codex: spawning exec | cwd={} model={} prompt_len={}",
            self.directory or "-",
            model.model_id if model is not None else "default",
            len(prompt),
        )
        self._logger().debug("codex: prompt | text={!r}", prompt)
        self._logger().debug("codex: exec cmd | cmd={}", cmd)

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
            self._logger().info(
                "codex: exec exited | returncode={}", self._process.returncode
            )

        async def read_output():
            if self._process:
                await self._read_events(self._process.stdout)

        async def read_stderr():
            if self._process:
                await self._read_stderr(self._process.stderr)

        self._reader_task = asyncio.create_task(read_output())
        self._stderr_task = asyncio.create_task(read_stderr())
        self._task = asyncio.create_task(run())
        await self._fan_out_state(AgentState.THINKING)

    async def cancel(self) -> None:
        if self._task is None and self._process is None:
            return
        self._logger().info("codex: cancelling turn")
        self._cancelled.set()

        if self._task is not None and not self._task.done():
            self._task.cancel()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

        with suppress(asyncio.CancelledError):
            if self._task is not None:
                await self._task

        with suppress(asyncio.CancelledError):
            if self._reader_task is not None:
                await self._reader_task

        with suppress(asyncio.CancelledError):
            if self._stderr_task is not None:
                await self._stderr_task

        if self._process is not None:
            if self._process.returncode is None:
                self._process.terminate()
            await self._process.wait()

        self._process = None
        self._task = None
        self._reader_task = None
        self._stderr_task = None
        await self._fan_out_state(AgentState.IDLE)
        self._text_accumulated = ""

    async def _read_events(self, stdout: asyncio.StreamReader | None) -> None:
        if stdout is None:
            self._logger().warning("codex: stdout stream missing")
            return
        while True:
            try:
                line = await asyncio.wait_for(stdout.readline(), timeout=60.0)
            except asyncio.CancelledError:
                self._logger().debug("codex: event reader cancelled")
                break
            except TimeoutError:
                self._logger().warning("codex: event reader timed out waiting for stdout")
                break
            if not line:
                self._logger().debug("codex: event stream ended")
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self._logger().warning("codex: ignored non-json stdout line | line={!r}", line)
                continue
            self._logger().debug("codex: event | payload={}", event)
            await self._handle_event(event)

    async def _read_stderr(self, stderr: asyncio.StreamReader | None) -> None:
        if stderr is None:
            return
        while True:
            try:
                line = await stderr.readline()
            except asyncio.CancelledError:
                break
            if not line:
                break
            self._logger().warning("codex: stderr | line={}", line.decode(errors="replace").strip())

    async def _handle_event(self, event: dict[str, Any]) -> None:  # noqa: PLR0912, PLR0915
        event_type = event.get("type", "")

        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if thread_id:
                self._assign_thread_id(thread_id)
            self._logger().info("codex: thread started")

        elif event_type == "turn.started":
            self._turn_id = event.get("turn_id")
            self._logger().info("codex: turn started")
            await self._fan_out_state(AgentState.THINKING)

        elif event_type == "turn.completed":
            self._logger().info(
                "codex: turn completed | response_len={}", len(self._text_accumulated)
            )
            self._logger().debug("codex: response | text={!r}", self._text_accumulated)
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
                self._logger().error("codex: turn failed | message={}", message)
                await self._fan_out_error(message)
            else:
                self._logger().error("codex: turn failed without message | event={}", event)
            await self._fan_out_state(AgentState.IDLE)

        elif event_type == "item.started":
            item = event.get("item", {})
            item_type = item.get("type", "")
            item_id = item.get("id", "")

            if item_type == "agent_message":
                pass

            elif item_type == "command_execution":
                command = item.get("command", "")
                self._logger(item_id=item_id or "-").info(
                    "codex: command execution started | command={}", command
                )
                for handler in tuple(self._tool_start_handlers):
                    await handler("bash", {"command": command, "item_id": item_id})

            elif item_type == "tool_use":
                tool_name = item.get("name", "")
                tool_input = event.get("input", {})
                if tool_name and tool_name not in self._announced_tools:
                    self._announced_tools.add(tool_name)
                    self._logger(item_id=item_id or "-").info(
                        "codex: tool started | tool={}", tool_name
                    )
                    for handler in tuple(self._tool_start_handlers):
                        await handler(tool_name, tool_input)

        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")

            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    self._text_accumulated += text
                    self._logger().debug("codex: agent message chunk | chars={}", len(text))

            elif item_type == "command_execution":
                status = item.get("status", "")
                self._logger(item_id=item.get("id", "-")).info(
                    "codex: command execution completed | status={}", status or "-"
                )

            elif item_type == "tool_use":
                self._logger(item_id=item.get("id", "-")).info(
                    "codex: tool completed | tool={}", item.get("name", "-")
                )

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
        self._pending_sessions: list[CodexSession] = []

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
        session = CodexSession(title=title, directory=directory)
        self._pending_sessions.append(session)
        return session

    def attach(self, session_id: str) -> CodexSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = CodexSession(id=session_id, _thread_id=session_id)
        self._sessions[session_id] = session
        return session

    def register_session_by_sdk_id(self, session: CodexSession, sdk_id: str) -> None:
        """Index a pending session by Codex's thread UUID after the first turn starts."""
        self._sessions[sdk_id] = session
        self._pending_sessions = [
            pending for pending in self._pending_sessions if pending is not session
        ]

    # ── Persistence ────────────────────────────────────────────────────

    async def list_sessions(self, *, directory: str | None = None) -> list[SessionInfo]:
        _ = directory
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
                        sid = _read_codex_session_id(session_file) or session_file.stem
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

    async def get_transcript(self, session_id: str) -> list[Message]:  # noqa: PLR0912
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
                    with open(matches[0]) as f:  # noqa: ASYNC230
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
        sessions = list(self._sessions.values()) + self._pending_sessions
        seen: set[int] = set()
        for session in sessions:
            if id(session) in seen:
                continue
            seen.add(id(session))
            await session.cancel()
        self._sessions.clear()
        self._pending_sessions.clear()
