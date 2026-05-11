from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

logger = logging.getLogger("friday.narrator_brain")

_DEFAULT_PROVIDER_SYSTEM = """You are the coding agent behind Friday's voice interface.
The user is speaking through Friday, a voice layer that relays their intent to you.
Treat each request as authoritative user intent and continue working in this provider session.
Keep final responses concise, factual, and suitable to be spoken aloud."""

_NARRATOR_DECISION_SYSTEM = """You are Friday's voice layer.

Write what Friday should say aloud right now.
The coding provider is the source of truth for the work and final answers.
Your `text` field goes directly to a text-to-speech engine and to the user's
conversation transcript.

Rules:
- Use only the provided JSON snapshot.
- Do not solve the coding task.
- Do not invent progress.
- Do not repeat what Friday already said.
- For progress checks, prefer provider reasoning events because they explain
  what the agent is trying to do. Translate them into user-facing progress.
- Do not announce raw tool names. Use the activity summary and input_summary to
  describe the user-level action.
- Write conversational prose, as if speaking to the user in the room.
- Prefer one sentence. Use two short sentences only when one would be unclear.
- Do not include Markdown or rich-text formatting: no headings, bullets,
  numbered lists, tables, code fences, backticks, Markdown links, block quotes,
  bold/italic markers, or labels like "Run via:".
- Do not dump tool names, plugin names, package names, command output, file
  contents, diffs, logs, JSON, or schema details unless the user specifically
  asked for that exact detail.
- If the provider's final answer is structured, Markdown-heavy, code-heavy, or
  implementation-detail-heavy, translate it into a short plain-English summary
  of the useful result for the user.
- Return only JSON matching the requested schema."""

_NARRATOR_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
    },
    "required": ["text"],
    "additionalProperties": False,
}

NarratorDecisionType = Literal["progress_check", "final_response"]
NarratorDecisionAction = Literal["speak", "silent"]
ChatRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: ChatRole
    content: str


class JsonChatClient(Protocol):
    async def complete_json(
        self,
        *,
        messages: Sequence[ChatMessage],
        schema_name: str,
        json_schema: dict[str, Any],
        temperature: float,
    ) -> dict[str, Any]:
        """Return parsed JSON from a chat-completions request."""
        ...

    async def aclose(self) -> None:
        """Close client resources."""
        ...


@dataclass(frozen=True, slots=True)
class NarratorDecision:
    action: NarratorDecisionAction
    text: str | None = None


def normalize_spoken_text(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    return stripped


class NarratorBrain(Protocol):
    @property
    def provider_system(self) -> str:
        """System prompt sent to the coding provider with each user turn."""
        ...

    async def decide(self, snapshot: dict[str, Any]) -> NarratorDecision:
        """Decide whether Friday should speak for a narration snapshot."""
        ...

    async def aclose(self) -> None:
        """Clean up brain resources."""
        ...


class EventedNarratorBrain:
    """Default narrator: no ack, no progress chatter, speak safe final answers."""

    @property
    def provider_system(self) -> str:
        return _DEFAULT_PROVIDER_SYSTEM

    async def decide(self, snapshot: dict[str, Any]) -> NarratorDecision:
        provider_context = snapshot.get("provider_context")
        if not isinstance(provider_context, dict):
            return NarratorDecision(action="silent")

        if snapshot.get("decision_type") == "progress_check":
            spoken_text = normalize_spoken_text(_latest_activity_summary(provider_context))
            if spoken_text is None:
                return NarratorDecision(action="silent")
            return NarratorDecision(action="speak", text=spoken_text)

        if snapshot.get("decision_type") != "final_response":
            return NarratorDecision(action="silent")

        final_text = provider_context.get("final_text")
        if not isinstance(final_text, str) or not final_text.strip():
            return NarratorDecision(action="silent")
        spoken_text = normalize_spoken_text(spoken_final_fallback(final_text))
        if spoken_text is None:
            return NarratorDecision(action="silent")
        return NarratorDecision(action="speak", text=spoken_text)

    async def aclose(self) -> None:
        return None


class JsonNarratorBrain:
    """LLM-backed narrator using a JSON-producing chat client."""

    def __init__(
        self,
        *,
        chat_client: JsonChatClient,
        fallback: NarratorBrain | None = None,
    ) -> None:
        self._chat_client = chat_client
        self._fallback = fallback or EventedNarratorBrain()

    @property
    def provider_system(self) -> str:
        return _DEFAULT_PROVIDER_SYSTEM

    async def decide(self, snapshot: dict[str, Any]) -> NarratorDecision:
        try:
            parsed = await self._chat_client.complete_json(
                messages=[
                    ChatMessage(role="system", content=_NARRATOR_DECISION_SYSTEM),
                    ChatMessage(
                        role="user",
                        content=(
                            "Here is the current narration decision snapshot:\n"
                            f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
                        ),
                    ),
                ],
                schema_name="narrator_decision",
                json_schema=_NARRATOR_DECISION_SCHEMA,
                temperature=0.4,
            )
            text = parsed.get("text")
            if not isinstance(text, str):
                raise ValueError("narrator text must be a string")
            spoken_text = normalize_spoken_text(text)
            if spoken_text is None:
                raise ValueError("narrator text cannot be empty")
            if not _is_plain_spoken_text(spoken_text):
                return await self._fallback.decide(snapshot)
            return NarratorDecision(action="speak", text=spoken_text)
        except Exception as err:
            logger.warning("narrator LLM decision failed; falling back | err=%s", err)
            return await self._fallback.decide(snapshot)

    async def aclose(self) -> None:
        await self._chat_client.aclose()
        await self._fallback.aclose()


def spoken_final_fallback(text: str) -> str | None:
    stripped = normalize_spoken_text(text)
    if stripped is None:
        return None
    if _is_plain_spoken_text(stripped):
        return stripped
    if _looks_like_question_with_options(stripped):
        return (
            "The agent finished and is asking you to choose between the options "
            "in the transcript."
        )
    return "The agent finished and posted a detailed result in the transcript."


def _latest_activity_summary(provider_context: dict[str, Any]) -> str | None:
    recent_events = provider_context.get("recent_events")
    if not isinstance(recent_events, list):
        return None
    for event in reversed(recent_events):
        if not isinstance(event, dict) or event.get("type") != "activity":
            continue
        summary = event.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    return None


def _is_plain_spoken_text(text: str) -> bool:
    if len(text) > 600:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 4:
        return False
    markdown_markers = ("```", "|", "##", "**", "`", "<task_result>", "</task_result>")
    if any(marker in text for marker in markdown_markers):
        return False
    structured_prefixes = ("-", "*", "1.", "2.", "3.", "###")
    if any(line.startswith(structured_prefixes) for line in lines):
        return False
    return True


def _looks_like_question_with_options(text: str) -> bool:
    lowered = text.lower()
    return "?" in text and any(
        phrase in lowered
        for phrase in (
            "do you want",
            "would you prefer",
            "which option",
            "choose between",
        )
    )
