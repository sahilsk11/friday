"""FastAPI app composition.

Owns the ``OpencodeClient`` lifecycle and a process-wide ``SessionManager``,
mounts the framework-neutral session API, and the WebRTC signaling router.

CORS: allows ``localhost``/``127.0.0.1`` on common Vite/Next dev ports so a
frontend dev server (default Vite is :5173) can hit the API directly. In
production the frontend is served from the same origin behind Caddy and CORS
is irrelevant. Override the allowed origins with ``FRIDAY_CORS_ORIGINS`` —
a comma-separated list, or ``*`` to allow any (only for debugging).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from friday.api.sessions import router as sessions_router
from friday.core.opencode_session import OpencodeClient
from friday.core.session_manager import SessionManager
from friday.voice.server import router as voice_router
from friday.voice.server import shutdown as voice_shutdown

DEFAULT_OPENCODE_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


@asynccontextmanager
async def default_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start the OpencodeClient (HTTP + SSE) and expose a SessionManager."""
    base_url = os.environ.get("OPENCODE_BASE_URL", DEFAULT_OPENCODE_BASE_URL)
    client = OpencodeClient(base_url)
    await client.start()
    app.state.client = client
    app.state.manager = SessionManager(client)
    try:
        yield
    finally:
        await voice_shutdown()
        await client.aclose()


def _resolve_cors_origins() -> list[str]:
    raw = os.environ.get("FRIDAY_CORS_ORIGINS")
    if raw is None:
        return DEFAULT_CORS_ORIGINS
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    """Build the FastAPI app. Tests pass ``with_lifespan=False`` and inject
    a ``SessionManager`` via ``app.dependency_overrides[get_manager]``.
    """
    app = FastAPI(title="friday", lifespan=default_lifespan if with_lifespan else None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_resolve_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    app.include_router(sessions_router)
    app.include_router(voice_router)
    return app


app = create_app()
