"""WebRTC offer/answer round-trip against ``/api/offer``.

Why this test exists: the previous round of refactoring introduced a 422 on
the offer endpoint because pipecat's ``SmallWebRTCRequest`` is a plain
``@dataclass`` that FastAPI's pydantic validator can't introspect. We never
caught it because there was no test that exercised the route with a real
SDP body. This test fills that gap — it uses ``aiortc`` (which pipecat
already depends on) to mint an actual RTCPeerConnection, generate a real
offer, post it, and verify the answer round-trips and applies cleanly.

We don't run the per-call voice pipeline during the test — that would spin
up Silero, the ElevenLabs websocket, etc. and isn't what we're verifying
here. We replace ``_run_pipeline`` with a no-op so the WebRTC handshake
happens but no media pipeline starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from aiortc import RTCPeerConnection, RTCSessionDescription
from httpx import ASGITransport

from friday.api.sessions import get_manager
from friday.core.opencode_session import OpencodeClient
from friday.core.session_manager import SessionManager
from friday.main import create_app
from friday.voice import server as voice_server

OPENCODE_URL = "http://opencode.test"


@pytest.fixture
async def manager() -> AsyncIterator[SessionManager]:
    client = OpencodeClient(OPENCODE_URL)
    yield SessionManager(client)
    await client.aclose()


@pytest.fixture
def app_with_stub_pipeline(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """FastAPI app with a no-op pipeline runner.

    The real ``_run_pipeline`` would load Silero, open ElevenLabs websockets,
    and try to talk to opencode. None of that is the offer endpoint's job.
    """
    monkeypatch.setattr(voice_server, "_run_pipeline", AsyncMock())
    app = create_app(with_lifespan=False)
    app.dependency_overrides[get_manager] = lambda: manager
    return app


async def test_offer_round_trips_a_real_sdp_offer(app_with_stub_pipeline: Any) -> None:
    """Mint a real SDP offer with aiortc, POST it, and apply the answer."""
    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    transport = ASGITransport(app=app_with_stub_pipeline)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://friday.test"
    ) as client:
        resp = await client.post(
            "/api/offer",
            json={
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "pc_id": None,
                "restart_pc": False,
                "request_data": {"session_id": None},
            },
        )

    try:
        assert resp.status_code == 200, f"offer rejected: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["type"] == "answer"
        assert body["sdp"].startswith("v=0")
        assert body.get("pc_id"), "answer should include a pc_id"

        # The real proof: the answer applies cleanly to our local peer.
        # If pipecat returned a malformed SDP, this would raise.
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=body["sdp"], type=body["type"])
        )
    finally:
        await pc.close()


async def test_offer_rejects_missing_sdp(app_with_stub_pipeline: Any) -> None:
    transport = ASGITransport(app=app_with_stub_pipeline)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://friday.test"
    ) as client:
        resp = await client.post("/api/offer", json={"type": "offer"})

    assert resp.status_code == 400
    assert "sdp" in resp.json()["detail"]


async def test_patch_round_trip_accepts_trickled_candidates(
    app_with_stub_pipeline: Any,
) -> None:
    """We can't trigger a real PATCH against an unknown pc_id (the handler
    rejects it), but we can verify the request body shape parses without 422.
    A 4xx that isn't 422 means our route accepted the body and the handler
    made its own decision.
    """
    transport = ASGITransport(app=app_with_stub_pipeline)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://friday.test"
    ) as client:
        resp = await client.patch(
            "/api/offer",
            json={
                "pc_id": "unknown_pc_id",
                "candidates": [
                    {
                        "candidate": "candidate:1 1 UDP 2013266431 192.0.2.1 12345 typ host",
                        "sdpMid": "0",
                        "sdpMLineIndex": 0,
                    }
                ],
            },
        )

    assert resp.status_code != 422, f"PATCH 422'd on a valid body: {resp.text}"
