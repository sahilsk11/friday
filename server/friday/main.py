"""FastAPI app composition — mounts the framework-neutral session API.

Voice signaling (Step 5) will mount onto this same app under its own prefix.
Until then this module only owns: the OpencodeClient lifecycle, the
SessionManager, and the ``/sessions`` router.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from friday.api.sessions import router as sessions_router
from friday.core.opencode_session import OpencodeClient
from friday.core.session_manager import SessionManager

DEFAULT_OPENCODE_BASE_URL = "http://127.0.0.1:4096"


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
        await client.aclose()


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    """Build the FastAPI app. Tests pass ``with_lifespan=False`` and inject
    a ``SessionManager`` via ``app.dependency_overrides[get_manager]``.
    """
    app = FastAPI(title="friday", lifespan=default_lifespan if with_lifespan else None)
    app.include_router(sessions_router)
    return app


app = create_app()
