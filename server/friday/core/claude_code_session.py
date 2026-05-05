"""ClaudeCode provider using Anthropic Agent SDK.

Wraps the Claude Agent SDK library to expose the same Provider + ProviderSession
interface as OpenCode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from friday.core.events import (
    MessagePartDelta,
    MessageUpdated,
    MessagePartUpdated,
    SessionIdle,
)
from friday.core.state import AgentState
from friday.core.provider import (
    ModelChoice,
    Provider,
    ProviderSession,
    TextDeltaHandler,
    TextFinalHandler,
    StateHandler,
    ToolStartHandler,
    Unsubscribe,
    _subscribe,
)


try:
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        SystemMessage,
        AssistantMessage,
        ResultMessage,
        StreamEvent,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    logger.warning("claude-agent-sdk not installed — ClaudeCodeProvider unavailable")


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
    _query_task: asyncio.Task | None = None
    _cancelled: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    id: str = ""
    current_state: AgentState = AgentState.IDLE

    _delta_handlers: list[TextDeltaHandler] = field(default_factory=list, repr=False)
    _final_handlers: list[TextFinalHandler] = field(default_factory=list, repr=False)
    _state_handlers: list[StateHandler] = field(default_factory=list, repr=False)
    _tool_start_handlers: list[ToolStartHandler] = field(default_factory=list, repr=False)

    _text_accumulated: str = ""
    _announced_tools: set[str] = field(default_factory=set, repr=False)

    def on_text_delta(self, handler: TextDeltaHandler) -> Unsubscribe:
        return _subscribe(self._delta_handlers, handler)

    def on_text_final(self, handler: TextFinalHandler) -> Unsubscribe:
        return _subscribe(self._final_handlers, handler)

    def on_state(self, handler: StateHandler) -> Unsubscribe:
        return _subscribe(self._state_handlers, handler)

    def on_tool_start(self, handler: ToolStartHandler) -> Unsubscribe:
        return _subscribe(self._tool_start_handlers, handler)

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
        opts = ClaudeAgentOptions(
            allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep", "WebFetch"],
            include_partial_messages=True,
        )

        if system:
            opts.append_system_prompt = system

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
        if isinstance(msg, SystemMessage):
            await self._handle_system(msg)
        elif isinstance(msg, AssistantMessage):
            await self._handle_assistant(msg)
        elif isinstance(msg, StreamEvent):
            await self._handle_stream(msg)
        elif isinstance(msg, ResultMessage):
            await self._handle_result(msg)

    async def _handle_system(self, msg: SystemMessage) -> None:
        if msg.subtype == "init":
            self.id = msg.data.get("session_id", "")

    async def _handle_assistant(self, msg: AssistantMessage) -> None:
        for block in msg.content:
            if hasattr(block, "type") and block.type == "tool_use":
                tool_name = getattr(block, "name", "")
                if tool_name and tool_name not in self._announced_tools:
                    self._announced_tools.add(tool_name)
                    tool_input = getattr(block, "input", {})
                    for handler in tuple(self._tool_start_handlers):
                        await handler(tool_name, tool_input)

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


class ClaudeCodeProvider:
    """Provider implementation wrapping Claude Agent SDK.

    Requires `pip install claude-agent-sdk`.
    """

    def __init__(self) -> None:
        if not SDK_AVAILABLE:
            raise RuntimeError(
                "claude-agent-sdk not installed. Install with: pip install claude-agent-sdk"
            )

    @property
    def provider_id(self) -> str:
        return "claude-code"

    async def create_session(self, title: str | None = None) -> ClaudeCodeSession:
        return ClaudeCodeSession(_http=None)

    async def aclose(self) -> None:
        pass