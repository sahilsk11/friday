"""Codex provider — implements the Provider/ProviderSession protocols.

Wraps the Codex CLI (codex exec) to expose the same interface as OpenCode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from friday.domain.provider import (
    ErrorHandler,
    Message,
    ModelCatalog,
    ModelChoice,
    ModelInfo,
    ReasoningHandler,
    SessionIdHandler,
    SessionInfo,
    SessionNotFound,
    StateHandler,
    TextDeltaHandler,
    TextFinalHandler,
    ToolStartHandler,
    Unsubscribe,
    subscribe,
)
from friday.domain.state import AgentState

logger = logging.getLogger("friday.codex_provider")

_CODEX_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CODEX_SESSION_ID_IN_TEXT_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
_CODEX_SESSION_DIR = _CODEX_HOME / "sessions"


@dataclass
class CodexSession:
    """One Codex session wrapping the codex exec subprocess."""

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
    _reasoning_handlers: list[ReasoningHandler] = field(default_factory=list, repr=False)
    _session_id_handlers: list[SessionIdHandler] = field(default_factory=list, repr=False)
    _state_handlers: list[StateHandler] = field(default_factory=list, repr=False)
    _tool_start_handlers: list[ToolStartHandler] = field(default_factory=list, repr=False)
    _error_handlers: list[ErrorHandler] = field(default_factory=list, repr=False)

    _text_accumulated: str = ""
    _announced_tools: set[str] = field(default_factory=set, repr=False)

    def on_text_delta(self, handler: TextDeltaHandler) -> Unsubscribe:
        return subscribe(self._delta_handlers, handler)

    def on_text_final(self, handler: TextFinalHandler) -> Unsubscribe:
        return subscribe(self._final_handlers, handler)

    def on_reasoning(self, handler: ReasoningHandler) -> Unsubscribe:
        return subscribe(self._reasoning_handlers, handler)

    def on_session_id(self, handler: SessionIdHandler) -> Unsubscribe:
        return subscribe(self._session_id_handlers, handler)

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

        if _is_codex_session_id(self.id):
            cmd = ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
            cmd.append(self.id)
        else:
            cmd = [
                "codex",
                "exec",
                "--json",
                "-s",
                "danger-full-access",
                "--skip-git-repo-check",
            ]
        if self.directory and not _is_codex_session_id(self.id):
            cmd.extend(["--cd", self.directory])
        if not _is_codex_session_id(self.id) and model is not None and model.model_id:
            cmd.extend(["--model", model.model_id])

        prompt = f"{system}\n\n{text}" if system else text

        logger.debug("codex exec cmd: %s", cmd)

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def run() -> None:
            if self._process is None or self._process.stdin is None:
                return
            self._process.stdin.write(prompt.encode())
            await self._process.stdin.drain()
            self._process.stdin.close()
            await self._process.wait()

        async def read_output() -> None:
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
            self._thread_id = string_value(event.get("thread_id"))
            await self._set_provider_session_id(self._thread_id)

        elif event_type == "session_meta":
            payload = record_value(event.get("payload"))
            await self._set_provider_session_id(string_value(payload.get("id")))

        elif event_type == "turn.started":
            self._turn_id = event.get("turn_id")
            await self._fan_out_state(AgentState.THINKING)

        elif event_type == "turn.completed":
            self._turn_id = None
            if self._text_accumulated:
                for handler in tuple(self._final_handlers):
                    await handler(self._text_accumulated)
            await self._fan_out_state(AgentState.IDLE)

        elif event_type == "item.started":
            item = event.get("item", {})
            item_type = item.get("type", "")
            item_id = item.get("id", "")

            if item_type == "agent_message":
                pass

            elif item_type == "reasoning":
                text = item.get("text", "")
                if text:
                    for reasoning_handler in tuple(self._reasoning_handlers):
                        await reasoning_handler(text)

            elif item_type == "command_execution":
                command = item.get("command", "")
                for tool_handler in tuple(self._tool_start_handlers):
                    await tool_handler("bash", {"command": command, "item_id": item_id})

            elif item_type == "tool_use":
                tool_name = item.get("name", "")
                tool_input = record_value(item.get("input")) or record_value(event.get("input"))
                if tool_name and tool_name not in self._announced_tools:
                    self._announced_tools.add(tool_name)
                    for tool_handler in tuple(self._tool_start_handlers):
                        await tool_handler(tool_name, tool_input)

        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")

            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    self._text_accumulated += text

            elif item_type == "reasoning":
                text = item.get("text", "")
                if text:
                    for reasoning_handler in tuple(self._reasoning_handlers):
                        await reasoning_handler(text)

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

    async def _set_provider_session_id(self, session_id: str) -> None:
        if not _is_codex_session_id(session_id) or session_id == self.id:
            return
        self.id = session_id
        for handler in tuple(self._session_id_handlers):
            await handler(session_id)

    async def _fan_out_error(self, message: str) -> None:
        for handler in tuple(self._error_handlers):
            await handler(message)


_CODEX_MODELS: list[ModelInfo] = [
    ModelInfo(
        provider_id="openai",
        provider_name="OpenAI",
        model_id="gpt-5.5",
        model_name="GPT-5.5 Codex",
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
        model_id="gpt-4-mini",
        model_name="GPT-4 Mini",
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
        model_id="gpt-5.3-codex-spark",
        model_name="GPT-5.3 Codex Spark",
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
    """Provider implementation wrapping the Codex CLI."""

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
        session = CodexSession(id=uuid4().hex, title=title, directory=directory)
        self._sessions[session.id] = session
        session.on_session_id(lambda session_id: self._reindex_session(session, session_id))
        return session

    def attach(self, session_id: str) -> CodexSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = CodexSession(id=session_id)
        self._sessions[session_id] = session
        session.on_session_id(lambda new_session_id: self._reindex_session(session, new_session_id))
        return session

    # ── Persistence ────────────────────────────────────────────────────

    async def list_sessions(self, *, directory: str | None = None) -> list[SessionInfo]:
        if not _CODEX_SESSION_DIR.exists():
            return []
        sessions: list[SessionInfo] = []
        for session_file in _iter_session_files():
            info = _parse_session_file_info(session_file)
            if info is None:
                continue
            if directory is not None and info.directory != directory:
                continue
            sessions.append(info)
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)[:50]

    async def get_session(self, session_id: str) -> SessionInfo:
        session_file = _find_session_file(session_id)
        if session_file is not None:
            info = _parse_session_file_info(session_file)
            if info is not None:
                return info
        raise SessionNotFound(f"codex session not found: {session_id}")

    async def get_transcript(self, session_id: str) -> list[Message]:
        session_file = _find_session_file(session_id)
        if session_file is None:
            return []
        return _parse_session_file_transcript(session_file)

    async def list_models(self) -> ModelCatalog:
        return ModelCatalog(models=list(_CODEX_MODELS), default=_CODEX_DEFAULT_MODEL)

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def aclose(self) -> None:
        for session in list(self._sessions.values()):
            await session.cancel()
        self._sessions.clear()

    async def _reindex_session(self, session: CodexSession, session_id: str) -> None:
        for key, value in list(self._sessions.items()):
            if value is session and key != session_id:
                self._sessions.pop(key, None)
        self._sessions[session_id] = session


def _is_codex_session_id(value: str | None) -> bool:
    return bool(value and _CODEX_SESSION_ID_RE.match(value))


def _iter_session_files() -> list[Path]:
    if not _CODEX_SESSION_DIR.exists():
        return []
    return sorted(_CODEX_SESSION_DIR.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime)


def _find_session_file(session_id: str) -> Path | None:
    if not _CODEX_SESSION_DIR.exists():
        return None
    matches = [
        path
        for path in _CODEX_SESSION_DIR.rglob("*.jsonl")
        if session_id in path.stem
    ]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _parse_session_file_info(path: Path) -> SessionInfo | None:
    session_id = ""
    directory = ""
    created_at: datetime | None = None
    title = "Codex session"

    for record in _iter_jsonl_records(path):
        timestamp = _parse_timestamp(record.get("timestamp"))
        if created_at is None and timestamp is not None:
            created_at = timestamp
        if record.get("type") != "session_meta":
            continue
        payload = record_value(record.get("payload"))
        session_id = string_value(payload.get("id"))
        directory = string_value(payload.get("cwd"))
        created_at = _parse_timestamp(payload.get("timestamp")) or timestamp or created_at
        break

    if not _is_codex_session_id(session_id):
        session_id = _session_id_from_filename(path)
    if not _is_codex_session_id(session_id):
        return None

    if not directory:
        directory = str(path.parent)
    if created_at is None:
        created_at = datetime.fromtimestamp(path.stat().st_ctime, tz=UTC)

    return SessionInfo(
        id=session_id,
        title=title,
        directory=directory,
        created_at=created_at,
        updated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
    )


def _parse_session_file_transcript(path: Path) -> list[Message]:
    messages: list[Message] = []
    current_model: ModelChoice | None = None

    for record in _iter_jsonl_records(path):
        if record.get("type") == "session_meta":
            current_model = _model_from_session_meta(record)
            continue
        if record.get("type") != "response_item":
            continue
        payload = record_value(record.get("payload"))
        parsed = _message_from_response_item(payload, record, current_model)
        if parsed is not None:
            messages.append(parsed)

    return messages


def _message_from_response_item(
    payload: dict[str, Any],
    record: dict[str, Any],
    model: ModelChoice | None,
) -> Message | None:
    item_type = string_value(payload.get("type"))
    completed_at = _parse_timestamp(record.get("timestamp"))

    if item_type == "message":
        role = string_value(payload.get("role"))
        if role not in {"user", "assistant"}:
            return None
        parts = _content_parts(payload.get("content"))
        text = _parts_text(parts)
        if role == "user":
            text = _clean_codex_user_text(text)
            if text:
                parts = [{"type": "text", "text": text}]
            else:
                parts = []
        if not text and not parts:
            return None
        return Message(
            role=role,
            text=text,
            completed_at=completed_at,
            parts=parts,
            model=model if role == "assistant" else None,
        )

    if item_type == "function_call":
        tool_name = string_value(payload.get("name")) or "tool"
        arguments = _parse_json_object(string_value(payload.get("arguments")))
        part = {
            "type": "tool",
            "name": tool_name,
            "status": "running",
            "input": arguments if arguments is not None else string_value(payload.get("arguments")),
        }
        return Message(
            role="assistant",
            text=f"Using {tool_name}",
            completed_at=completed_at,
            parts=[part],
            model=model,
        )

    if item_type == "function_call_output":
        output = string_value(payload.get("output"))
        call_id = string_value(payload.get("call_id"))
        if not output:
            return None
        return Message(
            role="tool",
            text=output,
            completed_at=completed_at,
            parts=[
                {
                    "type": "tool_result",
                    "call_id": call_id,
                    "output": output,
                }
            ],
        )

    if item_type == "reasoning":
        text = _reasoning_text(payload)
        if not text:
            return None
        return Message(
            role="assistant",
            text=text,
            completed_at=completed_at,
            parts=[{"type": "reasoning", "text": text}],
            model=model,
        )

    return None


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = string_value(block.get("type"))
        text = string_value(block.get("text"))
        if block_type in {"input_text", "output_text", "text"} and text:
            parts.append({"type": "text", "text": text})
        elif block_type:
            part = dict(block)
            parts.append(part)
    return parts


def _parts_text(parts: list[dict[str, Any]]) -> str:
    return "\n".join(
        text
        for text in (string_value(part.get("text")) for part in parts)
        if text
    ).strip()


def _clean_codex_user_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("<environment_context>"):
        return ""
    friday_prefix = "You are the coding agent behind Friday's voice interface."
    if stripped.startswith(friday_prefix) and "\n\n" in stripped:
        return stripped.split("\n\n", 1)[1].strip()
    return stripped


def _reasoning_text(payload: dict[str, Any]) -> str:
    summaries = payload.get("summary")
    if isinstance(summaries, list):
        text = "\n".join(
            string_value(item.get("text"))
            for item in summaries
            if isinstance(item, dict) and string_value(item.get("text"))
        ).strip()
        if text:
            return text
    return string_value(payload.get("text")) or string_value(payload.get("content"))


def _model_from_session_meta(record: dict[str, Any]) -> ModelChoice | None:
    payload = record_value(record.get("payload"))
    provider_id = string_value(payload.get("model_provider"))
    model_id = string_value(payload.get("model"))
    if provider_id and model_id:
        return ModelChoice(provider_id=provider_id, model_id=model_id)
    return None


def _session_id_from_filename(path: Path) -> str:
    match = _CODEX_SESSION_ID_IN_TEXT_RE.search(path.stem)
    return match.group(0) if match else ""


def _iter_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_json_object(value: str) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def record_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
