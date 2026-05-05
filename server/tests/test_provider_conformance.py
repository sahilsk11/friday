"""Runtime conformance: concrete sessions/clients implement the Protocols.

Covers the structural contract that ``provider.py`` is supposed to enforce.
Without these tests, a refactor that drops one of the protocol methods on
a concrete class wouldn't be caught until the live pipeline runs.
"""

from __future__ import annotations

import httpx

from friday.core.claude_code_session import ClaudeCodeProvider, ClaudeCodeSession
from friday.core.opencode_session import OpencodeClient, OpencodeSession
from friday.core.provider import Provider, ProviderSession


def test_opencode_session_is_provider_session() -> None:
    http = httpx.AsyncClient()
    session = OpencodeSession(http, "ses_test")
    assert isinstance(session, ProviderSession)


def test_opencode_client_is_provider() -> None:
    client = OpencodeClient("http://test.invalid")
    assert isinstance(client, Provider)
    assert client.provider_id == "opencode"


def test_claude_code_session_is_provider_session() -> None:
    session = ClaudeCodeSession(_http=None)
    assert isinstance(session, ProviderSession)


def test_claude_code_provider_is_provider() -> None:
    provider = ClaudeCodeProvider()
    assert isinstance(provider, Provider)
    assert provider.provider_id == "claude-code"
