"""Generate natural language descriptions for tool calls.

When OPENROUTER_API_KEY is set, calls a cheap model to produce a specific
phrase like "reading ActivityFeed.tsx" from the raw tool name and input args.
Falls back to the static checkpoint mapping in narration_policy when the key
is absent or the call fails.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from loguru import logger

from friday.core.narration_policy import checkpoint_for_tool

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL = "openai/gpt-4o-mini"
_TIMEOUT = 3.0

_SYSTEM = (
    "You narrate tool calls for a voice coding assistant. "
    "Reply with ONE phrase, 3–6 words, present continuous (e.g. 'reading', 'running', 'searching'). "
    "ALWAYS include the actual value from the input: file path, command, glob pattern, or search query. "
    "Use the basename for paths (e.g. 'reading App.tsx', not 'reading src/components/App.tsx'). "
    "Quote commands and patterns verbatim (e.g. 'running git status', 'searching for **/*.ts'). "
    "NO filler words like 'the', 'a', 'in directory', 'specified path', 'current directory'. "
    "No punctuation at the end. No markdown. No explanation. No quotes around the phrase itself."
)


async def describe_tool(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Return a short spoken phrase for a tool call, or None to stay silent.

    With OPENROUTER_API_KEY set: always returns a string (LLM-generated).
    Without it: returns the static checkpoint phrase, or None for unknown tools.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    logger.info("tool_narrator: describe_tool | tool={} has_key={}", tool_name, bool(api_key))
    if not api_key:
        return checkpoint_for_tool(tool_name)

    user_msg = f"Tool: {tool_name}\nInput: {json.dumps(tool_input)}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": _MODEL,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 30,
                },
            )
            resp.raise_for_status()
            content: str = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            logger.info("tool_narrator: got label | tool={} label={!r}", tool_name, content)
            return content or checkpoint_for_tool(tool_name)
    except Exception as err:
        logger.warning("tool_narrator: OpenRouter call failed | tool={} err={}", tool_name, err)
        return checkpoint_for_tool(tool_name)
