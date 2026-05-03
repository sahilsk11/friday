"""AgentState enum — listening / thinking / speaking lifecycle."""

from __future__ import annotations

from enum import StrEnum


class AgentState(StrEnum):
    """Coarse lifecycle state surfaced to UI consumers.

    The voice layer maps user-speaking → ``LISTENING``, opencode-busy →
    ``THINKING``, TTS-active → ``SPEAKING``, otherwise ``IDLE``.
    """

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
