"""Tests for ack_generator.

The contract: ``generate_ack`` always returns a non-empty string. With
``OPENROUTER_API_KEY`` set, it returns the model's phrase (cleaned of
trailing punctuation and surrounding quotes). Without the key, on HTTP
failure, or on empty model output, it returns the static fallback.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from friday.core.ack_generator import FALLBACK, OPENROUTER_URL, generate_ack


def _ok(content: str) -> dict[str, object]:
    """Shape an OpenRouter chat-completions response with ``content``."""
    return {"choices": [{"message": {"content": content}}]}


async def test_no_api_key_returns_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert await generate_ack("fix the auth bug") == FALLBACK


async def test_empty_transcript_returns_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    # Whitespace-only is treated as empty.
    assert await generate_ack("   \n\t") == FALLBACK


async def test_returns_model_phrase(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    httpx_mock.add_response(
        method="POST",
        url=OPENROUTER_URL,
        json=_ok("alright, looking into the auth flow"),
    )

    assert await generate_ack("can you fix the auth bug") == "alright, looking into the auth flow"


async def test_strips_surrounding_quotes_and_trailing_punct(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Models occasionally wrap their answer in quotes despite the system
    prompt — strip them so TTS doesn't read 'quote ... unquote'."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    httpx_mock.add_response(
        method="POST",
        url=OPENROUTER_URL,
        json=_ok('"sure, taking a look."'),
    )

    assert await generate_ack("explain this function") == "sure, taking a look"


async def test_http_error_falls_back(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    httpx_mock.add_response(method="POST", url=OPENROUTER_URL, status_code=500)

    assert await generate_ack("anything") == FALLBACK


async def test_empty_model_content_falls_back(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """If the model returns an empty string (or only punctuation that gets
    stripped), we'd rather speak the fallback than nothing at all."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    httpx_mock.add_response(method="POST", url=OPENROUTER_URL, json=_ok(""))

    assert await generate_ack("anything") == FALLBACK
