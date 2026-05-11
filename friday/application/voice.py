"""Voice-agent use case helpers.

This module intentionally avoids LiveKit, HTTP, and speech vendor imports. The
voice delivery adapter owns those concrete concerns and calls these helpers for
backend client contracts, event de-duplication, and narrator playback decisions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class NarratorEvent:
    id: int
    type: str
    text: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


class NarratorBackendClient(Protocol):
    async def submit_turn(
        self,
        *,
        session_id: str,
        source: str = "voice",
        text: str,
    ) -> list[NarratorEvent]: ...

    async def cancel(self, *, session_id: str) -> list[NarratorEvent]: ...

    async def list_events(
        self,
        *,
        session_id: str,
        after_id: int,
        limit: int = 50,
    ) -> list[NarratorEvent]: ...

    async def aclose(self) -> None: ...


VoiceAgentMessageType = Literal[
    "transcript",
    "text_delta",
    "text_final",
    "state",
    "tool_start",
    "narration",
    "error",
]


@dataclass(frozen=True, slots=True)
class VoiceAgentMessage:
    type: VoiceAgentMessageType
    event_id: int | None = None
    text: str | None = None
    state: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceEventHandling:
    messages: list[VoiceAgentMessage]
    speech_text: str | None = None


class NarratorEventRelay:
    def __init__(self) -> None:
        self.cursor = 0
        self._seen: set[int] = set()
        self._lock = asyncio.Lock()

    async def unseen(self, events: list[NarratorEvent]) -> list[NarratorEvent]:
        async with self._lock:
            unseen: list[NarratorEvent] = []
            for event in events:
                self.cursor = max(self.cursor, event.id)
                if event.id in self._seen:
                    continue
                self._seen.add(event.id)
                unseen.append(event)
            return unseen


class VoicePlaybackState:
    def __init__(self) -> None:
        self.user_turn_open = False
        self.speaker_enabled = True
        self.session_error_message: str | None = None
        self.command_lock = asyncio.Lock()


class VoiceInteractionService:
    def handle_narrator_event(
        self,
        event: NarratorEvent,
        playback_state: VoicePlaybackState,
    ) -> VoiceEventHandling:
        if event.type in {"speech", "progress"}:
            if event.text:
                return VoiceEventHandling(
                    messages=[
                        VoiceAgentMessage(
                            type="narration",
                            event_id=event.id,
                            text=event.text,
                        )
                    ],
                    speech_text=(event.text if self._should_speak(playback_state) else None),
                )
            return VoiceEventHandling(messages=[])

        if event.type == "final":
            if event.text:
                return VoiceEventHandling(
                    messages=[
                        VoiceAgentMessage(
                            type="text_final",
                            event_id=event.id,
                            text=event.text,
                        )
                    ],
                    speech_text=(event.text if self._should_speak(playback_state) else None),
                )
            return VoiceEventHandling(messages=[])

        if event.type == "error":
            return VoiceEventHandling(
                messages=[
                    VoiceAgentMessage(
                        type="error",
                        event_id=event.id,
                        message=event.text or "Unknown narrator error",
                    )
                ]
            )

        if event.type == "state":
            state = event.payload.get("state")
            if isinstance(state, str):
                return VoiceEventHandling(
                    messages=[
                        VoiceAgentMessage(
                            type="state",
                            event_id=event.id,
                            state=state,
                        )
                    ]
                )

        return VoiceEventHandling(messages=[])

    def _should_speak(self, playback_state: VoicePlaybackState) -> bool:
        return (
            not playback_state.user_turn_open
            and playback_state.speaker_enabled
            and playback_state.session_error_message is None
        )


__all__ = [
    "NarratorBackendClient",
    "NarratorEvent",
    "NarratorEventRelay",
    "VoiceAgentMessage",
    "VoiceEventHandling",
    "VoiceInteractionService",
    "VoicePlaybackState",
]
