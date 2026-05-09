"""HTTP + SSE tests for the sessions router.

The router talks to opencode via ``OpencodeProvider.http``. Tests inject a
non-started OpencodeProvider (no SSE loop) wrapped in a ProviderRegistry and
let pytest-httpx canned responses stand in for opencode HTTP.

For SSE we drive synthetic events directly into the cached
``OpencodeSession`` via ``dispatch()`` — the SSE generator's observers fire
and frames flow out the response stream.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport
from pytest_httpx import HTTPXMock

from friday.api.sessions import get_registry, stream_events
from friday.core.events import (
    MessagePartDelta,
    MessageUpdated,
    SessionError,
    SessionStatus,
)
from friday.core.opencode_provider import OpencodeProvider, OpencodeSession
from friday.core.session_registry import ProviderRegistry
from friday.main import create_app

OPENCODE_URL = "http://opencode.test"


@pytest.fixture
async def provider() -> AsyncIterator[OpencodeProvider]:
    """A non-started OpencodeProvider (HTTP-only)."""
    client = OpencodeProvider(OPENCODE_URL)
    yield client
    await client.aclose()


@pytest.fixture
def registry(provider: OpencodeProvider) -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.add(provider)
    # Pre-register the session id used across most tests so resolve_for_session
    # returns immediately without probing opencode via HTTP.
    reg.register_session("ses_a", provider.provider_id)
    return reg


@pytest.fixture
def client(registry: ProviderRegistry) -> httpx.AsyncClient:
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_registry] = lambda: registry
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://friday.test")


async def test_list_sessions_returns_rows(httpx_mock: HTTPXMock, client: httpx.AsyncClient) -> None:
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/experimental/session",
        json=[
            {
                "id": "ses_a",
                "title": "alpha",
                "directory": "/x",
                "time": {"created": 1_700_000_000_000, "updated": 1_700_000_001_000},
            }
        ],
    )

    async with client:
        resp = await client.get("/sessions")

    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == "ses_a"
    assert rows[0]["title"] == "alpha"
    assert rows[0]["created_at"].startswith("2023-11-")


async def test_list_sessions_passes_directory_filter(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/experimental/session",
        json=[
            {
                "id": "ses_a",
                "title": "",
                "directory": "/keep",
                "time": {"created": 1, "updated": 1},
            },
            {
                "id": "ses_b",
                "title": "",
                "directory": "/skip",
                "time": {"created": 1, "updated": 1},
            },
        ],
    )

    async with client:
        resp = await client.get("/sessions", params={"directory": "/keep"})

    assert [r["id"] for r in resp.json()] == ["ses_a"]


async def test_get_session_returns_metadata_and_transcript(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session/ses_a",
        json={
            "id": "ses_a",
            "title": "t",
            "directory": "/x",
            "time": {"created": 1, "updated": 1},
        },
    )
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session/ses_a/message",
        json=[
            {
                "info": {"role": "user", "time": {"created": 1}, "id": "msg_1"},
                "parts": [{"type": "text", "text": "hello"}],
            },
            {
                "info": {
                    "role": "assistant",
                    "time": {"created": 1, "completed": 2_000},
                    "id": "msg_2",
                },
                "parts": [{"type": "text", "text": "Hi"}],
            },
        ],
    )

    async with client:
        resp = await client.get("/sessions/ses_a")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["id"] == "ses_a"
    assert [m["role"] for m in body["transcript"]] == ["user", "assistant"]
    assert body["transcript"][1]["text"] == "Hi"
    assert body["transcript"][1]["completed_at"] is not None


async def test_post_turn_forwards_to_opencode(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/ses_a/prompt_async",
        status_code=204,
    )

    async with client:
        resp = await client.post("/sessions/ses_a/turn", json={"text": "hi"})

    assert resp.status_code == 202
    assert resp.json() == {"session_id": "ses_a"}
    sent = httpx_mock.get_requests()[0]
    assert json.loads(sent.content) == {"parts": [{"type": "text", "text": "hi"}]}


async def test_sse_streams_delta_final_and_state(
    provider: OpencodeProvider, registry: ProviderRegistry
) -> None:
    """Drive the SSE route handler directly and consume its body iterator."""
    response = await stream_events(session_id="ses_a", registry=registry)
    assert response.media_type == "text/event-stream"

    session = provider.attach("ses_a")
    assert isinstance(session, OpencodeSession)
    await session.dispatch(SessionStatus(session_id="ses_a", status="busy"))
    await session.dispatch(
        MessagePartDelta(
            session_id="ses_a",
            message_id="msg_1",
            part_id="prt_1",
            field="text",
            delta="Hi",
        )
    )
    await session.dispatch(
        MessageUpdated(
            session_id="ses_a",
            message_id="msg_1",
            role="assistant",
            time_end=2_000,
        )
    )

    collected: list[dict[str, str]] = []
    async for raw in response.body_iterator:
        chunk = bytes(raw) if isinstance(raw, bytes | memoryview) else raw.encode("utf-8")
        collected.extend(
            json.loads(line[len(b"data:") :].strip())
            for line in chunk.splitlines()
            if line.startswith(b"data:")
        )
        if len(collected) >= 4:
            break

    types = [c["type"] for c in collected]
    assert types == ["state", "state", "text.delta", "text.final"]
    assert collected[0] == {"type": "state", "state": "idle"}
    assert collected[1] == {"type": "state", "state": "thinking"}
    assert collected[2] == {"type": "text.delta", "text": "Hi"}
    assert collected[3] == {"type": "text.final", "text": "Hi"}


async def test_sse_streams_provider_errors(
    provider: OpencodeProvider, registry: ProviderRegistry
) -> None:
    response = await stream_events(session_id="ses_a", registry=registry)
    session = provider.attach("ses_a")

    await session.dispatch(SessionError(session_id="ses_a", message="model cannot read images"))

    collected: list[dict[str, str]] = []
    async for raw in response.body_iterator:
        chunk = bytes(raw) if isinstance(raw, bytes | memoryview) else raw.encode("utf-8")
        collected.extend(
            json.loads(line[len(b"data:") :].strip())
            for line in chunk.splitlines()
            if line.startswith(b"data:")
        )
        if len(collected) >= 2:
            break

    assert collected == [
        {"type": "state", "state": "idle"},
        {"type": "error", "message": "model cannot read images"},
    ]


async def test_get_provider_503_when_unset() -> None:
    """If lifespan never ran and no override is registered, /sessions returns 503."""
    app = create_app(with_lifespan=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://friday.test") as c:
        resp = await c.get("/sessions")
    assert resp.status_code == 503


# ── model selection ─────────────────────────────────────────────────────────


async def test_post_turn_with_explicit_model_forwards_choice(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/ses_a/prompt_async",
        status_code=204,
    )

    async with client:
        resp = await client.post(
            "/sessions/ses_a/turn",
            json={"text": "hi", "model": {"providerID": "opencode", "modelID": "gpt-5-nano"}},
        )

    assert resp.status_code == 202
    sent = httpx_mock.get_requests()[0]
    assert json.loads(sent.content) == {
        "parts": [{"type": "text", "text": "hi"}],
        "model": {"providerID": "opencode", "modelID": "gpt-5-nano"},
    }


async def test_get_session_surfaces_current_model(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session/ses_a",
        json={
            "id": "ses_a",
            "title": "",
            "directory": "/x",
            "time": {"created": 1, "updated": 1},
        },
    )
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session/ses_a/message",
        json=[
            {
                "info": {
                    "role": "assistant",
                    "time": {"completed": 2_000},
                    "id": "msg_1",
                    "modelID": "z-ai/glm5",
                    "providerID": "nvidia",
                },
                "parts": [{"type": "text", "text": "Hi"}],
            },
        ],
    )

    async with client:
        resp = await client.get("/sessions/ses_a")

    assert resp.json()["current_model"] == {"providerID": "nvidia", "modelID": "z-ai/glm5"}


async def test_models_endpoint_filters_to_active_toolcall(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/config/providers",
        json={
            "default": {"opencode": "gpt-5-nano"},
            "providers": [
                {
                    "id": "opencode",
                    "name": "OpenCode Zen",
                    "models": {
                        "gpt-5-nano": {
                            "name": "GPT-5 Nano",
                            "status": "active",
                            "capabilities": {"toolcall": True},
                        },
                        "no-tools": {
                            "name": "No Tools",
                            "status": "active",
                            "capabilities": {"toolcall": False},
                        },
                        "deprecated": {
                            "name": "Deprecated",
                            "status": "deprecated",
                            "capabilities": {"toolcall": True},
                        },
                    },
                }
            ],
        },
    )

    async with client:
        resp = await client.get("/models")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default"] == {"providerID": "opencode", "modelID": "gpt-5-nano"}
    assert [m["modelID"] for m in body["models"]] == ["gpt-5-nano"]
    assert body["models"][0]["providerName"] == "OpenCode Zen"
    assert body["models"][0]["modelName"] == "GPT-5 Nano"
