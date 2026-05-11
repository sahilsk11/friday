"""Shared in-memory narrator workflow state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class NarrationState:
    latest_user_message: str | None = None
    turn_started_at: float | None = None
    last_spoken_at: float | None = None
    has_spoken_this_turn: bool = False
    partial_assistant_text: str = ""
    progress_task: asyncio.Task[None] | None = None
    last_progress_activity_event_id: int | None = None
    last_progress_tool_event_id: int | None = None
    last_progress_reasoning_event_id: int | None = None
    active_turn_id: str | None = None


__all__ = ["NarrationState"]
