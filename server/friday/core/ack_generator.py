"""Generate a contextual acknowledgment phrase for a finalized user turn.

Fires the moment STT finalizes — before opencode has had a chance to even
receive the prompt. Goal: a brief, natural phrase that proves we heard the
specific request, not a generic "on it" that's the same for every turn.

When ``OPENROUTER_API_KEY`` is set, calls a cheap model (mirrors the pattern
in ``tool_narrator``). Without the key, returns a static fallback so callers
never have to special-case the no-key path.
"""

from __future__ import annotations

import os

import httpx
from loguru import logger

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FALLBACK = "on it"
_MODEL = "openai/gpt-4o-mini"
# Tight budget on purpose: this phrase plays during user-perceived silence,
# so a slow ack is worse than the static fallback. 2s covers a healthy
# round-trip with margin; anything beyond that we're better off saying "on it".
_TIMEOUT = 2.0

_SYSTEM = (
    "You are a voice coding assistant. The user just spoke a request. "
    "Reply with ONE brief acknowledgment phrase (8 words max) that references "
    "what they asked, in a natural conversational tone for text-to-speech. "
    "Do NOT answer or solve the request — just acknowledge that you heard it "
    "and are about to start working. "
    "No greetings, no signoffs, no quotes around the phrase, no markdown, "
    "no trailing punctuation. "
    "Examples: "
    "request to fix a bug -> 'alright, let me dig into that'. "
    "request to explain code -> 'sure, taking a look'. "
    "request to refactor -> 'got it, working on it now'."
)


async def generate_ack(transcript: str) -> str:
    """Return a brief acknowledgment phrase for ``transcript``.

    Always returns a non-empty string. Falls back to a static phrase when
    the API key is missing, the call fails, or the model returns empty.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    logger.info("ack_generator: generate_ack | has_key={} len={}", bool(api_key), len(transcript))
    if not api_key or not transcript.strip():
        return FALLBACK
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": _MODEL,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": transcript},
                    ],
                    "max_tokens": 30,
                },
            )
            resp.raise_for_status()
            content: str = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            content = content.strip("\"'").rstrip(".!?")
            logger.info("ack_generator: got phrase | phrase={!r}", content)
            return content or FALLBACK
    except Exception as err:
        logger.warning("ack_generator: OpenRouter call failed | err={}", err)
        return FALLBACK
