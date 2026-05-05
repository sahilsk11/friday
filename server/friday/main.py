"""FastAPI app composition.

Owns the ``OpencodeProvider`` lifecycle and a process-wide ``SessionManager``,
mounts the framework-neutral session API, the WebSocket voice router, and
(when ``web/dist`` is built) the SPA bundle so a single uvicorn process
serves both the API and the frontend on one port.

CORS: allows ``localhost``/``127.0.0.1`` on common Vite/Next dev ports so a
frontend dev server (default Vite is :5173) can hit the API directly. In
production the frontend is served from the same origin and CORS is moot.
Override the allowed origins with ``FRIDAY_CORS_ORIGINS`` — a comma-separated
list, or ``*`` to allow any (only for debugging).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Repo root is two levels up from server/friday/main.py
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from friday.api.sessions import models_router, router as sessions_router
from friday.core.opencode_provider import OpencodeProvider
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
# repo_root/server/friday/main.py → repo_root/web/dist
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


@asynccontextmanager
async def default_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start the OpencodeProvider (HTTP + SSE) and expose a SessionManager."""
    base_url = os.environ.get("OPENCODE_BASE_URL", DEFAULT_OPENCODE_BASE_URL)
    provider = OpencodeProvider(base_url)
    await provider.start()
    app.state.provider = provider
    app.state.manager = SessionManager(provider)
    try:
        yield
    finally:
        await voice_shutdown()
        await provider.aclose()


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
    app.include_router(models_router)
    app.include_router(voice_router)
    _mount_spa(app)
    return app


async def _spa_fallback(full_path: str) -> FileResponse:
    candidate = WEB_DIST / full_path
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(WEB_DIST / "index.html")


def _mount_spa(app: FastAPI) -> None:
    """Serve the built frontend on the same origin as the API.

    Skipped when ``web/dist`` is absent (e.g. test envs that haven't run
    ``npm run build``). The catch-all returns ``index.html`` for any path
    that isn't a real file so client-side routes (``/s/:id`` etc.) work
    on direct loads. Registered last so API/WS routes match first.
    """
    if not WEB_DIST.is_dir():
        return
    assets_dir = WEB_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    app.add_api_route(
        "/{full_path:path}",
        _spa_fallback,
        methods=["GET"],
        include_in_schema=False,
    )


app = create_app()
