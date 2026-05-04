"""HTTP + SSE tests for the sessions router.

The router talks to opencode via ``SessionManager`` → ``OpencodeClient.http``.
Tests inject a manager wired to a non-started OpencodeClient (no SSE loop)
and let pytest-httpx canned responses stand in for opencode HTTP.

For SSE we drive synthetic events directly into the cached
``OpencodeSession`` via ``dispatch()`` — the SSE generator's observers fire
and frames flow out the response stream.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from pytest_httpx import HTTPXMock

from friday.api.sessions import get_manager, stream_events
from friday.core.events import (
    MessagePartDelta,
    MessageUpdated,
    SessionStatus,
)
from friday.core.opencode_session import OpencodeClient
from friday.core.session_manager import SessionManager
from friday.main import create_app

OPENCODE_URL = "http://opencode.test"


@pytest.fixture
async def manager() -> AsyncIterator[SessionManager]:
    """SessionManager wrapping a non-started OpencodeClient (HTTP-only)."""
    client = OpencodeClient(OPENCODE_URL)
    yield SessionManager(client)
    await client.aclose()


@pytest.fixture
def client(manager: SessionManager) -> httpx.AsyncClient:
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_manager] = lambda: manager
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://friday.test")


async def test_list_sessions_returns_rows(httpx_mock: HTTPXMock, client: httpx.AsyncClient) -> None:
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session",
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
        url=f"{OPENCODE_URL}/session",
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


async def test_create_session_posts_and_returns_metadata(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session",
        json={"id": "ses_new", "title": "fresh"},
    )
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session/ses_new",
        json={
            "id": "ses_new",
            "title": "fresh",
            "directory": "/x",
            "time": {"created": 1_700_000_000_000, "updated": 1_700_000_000_000},
        },
    )

    async with client:
        resp = await client.post("/sessions", json={"title": "fresh"})

    assert resp.status_code == 201
    assert resp.json()["id"] == "ses_new"
    assert resp.json()["title"] == "fresh"


async def test_create_session_with_valid_directory_forwards_query_param(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session?directory={tmp_path}",
        json={"id": "ses_new", "title": ""},
    )
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session/ses_new",
        json={
            "id": "ses_new",
            "title": "",
            "directory": str(tmp_path),
            "time": {"created": 1, "updated": 1},
        },
    )

    async with client:
        resp = await client.post("/sessions", json={"directory": str(tmp_path)})

    assert resp.status_code == 201


async def test_create_session_rejects_relative_directory(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.post("/sessions", json={"directory": "relative/path"})

    assert resp.status_code == 400
    assert "absolute" in resp.json()["detail"]


async def test_create_session_rejects_nonexistent_directory(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.post("/sessions", json={"directory": "/this/path/should/not/exist/xyz"})

    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]


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


async def test_sse_streams_delta_final_and_state(manager: SessionManager) -> None:
    """Drive the SSE route handler directly and consume its body iterator.

    We bypass ASGITransport here — it buffers streaming responses in-process,
    which deadlocks the test (the body generator is awaiting events that we
    can only dispatch after the response context manager opens, but the
    transport doesn't surface chunks until the generator returns). Calling the
    route function directly exercises the full observer-registration + queue
    + frame-packing path with deterministic ordering. The HTTP integration
    is verified end-to-end against live opencode in
    ``scripts/probe_api_sessions.py``.
    """
    response = await stream_events(session_id="ses_a", manager=manager)
    assert response.media_type == "text/event-stream"

    session = manager.attach("ses_a")
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
    assert types == ["state", "text.delta", "text.final", "state"]
    assert collected[0] == {"type": "state", "state": "thinking"}
    assert collected[1] == {"type": "text.delta", "text": "Hi"}
    assert collected[2] == {"type": "text.final", "text": "Hi"}
    assert collected[3] == {"type": "state", "state": "idle"}


async def test_get_manager_503_when_unset() -> None:
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


async def test_pre_first_turn_model_is_consumed_on_first_prompt(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Modal-supplied model on create flows through to opencode on the next turn."""
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session?directory={tmp_path}",
        json={"id": "ses_new", "title": ""},
    )
    httpx_mock.add_response(
        url=f"{OPENCODE_URL}/session/ses_new",
        json={
            "id": "ses_new",
            "title": "",
            "directory": str(tmp_path),
            "time": {"created": 1, "updated": 1},
        },
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/ses_new/prompt_async",
        status_code=204,
    )

    async with client:
        resp = await client.post(
            "/sessions",
            json={
                "directory": str(tmp_path),
                "model": {"providerID": "opencode", "modelID": "gpt-5-nano"},
            },
        )
        assert resp.status_code == 201
        # First turn — body has no `model`, but the cached choice should be
        # forwarded to opencode automatically.
        resp = await client.post("/sessions/ses_new/turn", json={"text": "hi"})
        assert resp.status_code == 202

    prompt_req = next(r for r in httpx_mock.get_requests() if r.url.path.endswith("/prompt_async"))
    assert json.loads(prompt_req.content) == {
        "parts": [{"type": "text", "text": "hi"}],
        "model": {"providerID": "opencode", "modelID": "gpt-5-nano"},
    }


async def test_patch_model_stages_for_next_turn(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    """Voice + REST share session.next_model — PATCH stages it, the next
    ``send_turn`` (from either path) carries it through."""
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/ses_a/prompt_async",
        status_code=204,
    )

    async with client:
        resp = await client.patch(
            "/sessions/ses_a/model",
            json={"providerID": "opencode", "modelID": "gpt-5-nano"},
        )
        assert resp.status_code == 204
        # Subsequent turn carries the staged model without a per-call override.
        resp = await client.post("/sessions/ses_a/turn", json={"text": "hi"})
        assert resp.status_code == 202

    prompt_req = next(r for r in httpx_mock.get_requests() if r.url.path.endswith("/prompt_async"))
    assert json.loads(prompt_req.content) == {
        "parts": [{"type": "text", "text": "hi"}],
        "model": {"providerID": "opencode", "modelID": "gpt-5-nano"},
    }


async def test_current_model_prefers_staged_over_last_assistant(
    httpx_mock: HTTPXMock, client: httpx.AsyncClient
) -> None:
    """After a fresh PATCH, the chip should show the staged model — not the
    one the *last* assistant message ran on. Otherwise the chip looks stuck."""
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
                    "time": {"completed": 1},
                    "id": "msg_1",
                    "modelID": "gpt-5-nano",
                    "providerID": "opencode",
                },
                "parts": [{"type": "text", "text": "hi"}],
            },
        ],
    )

    async with client:
        resp = await client.patch(
            "/sessions/ses_a/model",
            json={"providerID": "opencode", "modelID": "big-pickle"},
        )
        assert resp.status_code == 204
        resp = await client.get("/sessions/ses_a")

    assert resp.json()["current_model"] == {"providerID": "opencode", "modelID": "big-pickle"}


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


async def test_sse_emits_model_frame_on_assistant_completion(
    manager: SessionManager,
) -> None:
    response = await stream_events(session_id="ses_a", manager=manager)

    session = manager.attach("ses_a")
    await session.dispatch(
        MessageUpdated(
            session_id="ses_a",
            message_id="msg_1",
            role="assistant",
            time_end=2_000,
            model_id="gpt-5-nano",
            provider_id="opencode",
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
        if any(c["type"] == "model" for c in collected):
            break

    model_frames = [c for c in collected if c["type"] == "model"]
    assert model_frames == [{"type": "model", "providerID": "opencode", "modelID": "gpt-5-nano"}]


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
