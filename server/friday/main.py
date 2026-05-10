"""FastAPI app composition.

Owns the ``Provider`` lifecycle, mounts the framework-neutral session API,
the WebSocket voice router, and (when ``web/dist`` is built) the SPA bundle
so a single uvicorn process serves both the API and the frontend on one
port.

Both OpenCode and ClaudeCode providers are started at startup when their
respective dependencies are present. OpenCode requires ``opencode serve``
running at ``OPENCODE_BASE_URL`` (default: localhost:4096). ClaudeCode
requires ``ANTHROPIC_API_KEY``. If OpenCode is unreachable at startup it
is skipped; if ClaudeCode's key is absent it is skipped. At least one
provider must be available.

CORS: allows ``localhost``/``127.0.0.1`` on common Vite/Next dev ports so a
frontend dev server (default Vite is :5173) can hit the API directly. In
production the frontend is served from the same origin and CORS is moot.
Override the allowed origins with ``FRIDAY_CORS_ORIGINS`` — a comma-separated
list, or ``*`` to allow any (only for debugging).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from friday.api.config import router as config_router
from friday.api.sessions import harnesses_router, models_router
from friday.api.sessions import router as sessions_router
from friday.config import (
    FRIDAY_LOG_FILE,
    FRIDAY_LOG_LEVEL,
    FRIDAY_LOG_RETENTION,
    FRIDAY_LOG_ROTATION,
)
from friday.core.claude_code_provider import ClaudeCodeProvider
from friday.core.codex_provider import CodexProvider
from friday.core.opencode_provider import OpencodeProvider
from friday.core.session_registry import ProviderRegistry
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

# How long to wait for OpenCode's SSE stream before giving up and skipping it.
_OPENCODE_CONNECT_TIMEOUT = 5.0


def _log_format(record: Any) -> str:
    extra = record["extra"]
    session_id = extra.get("session_id", "-")
    turn_id = extra.get("turn_id", "-")
    item_id = extra.get("item_id", "-")
    return (
        "{time:YYYY-MM-DDTHH:mm:ss.SSSZZ} | {level} | {name}:{function}:{line} | "
        f"session={session_id} turn={turn_id} item={item_id} | "
        "{message}\n{exception}"
    )


def configure_logging() -> None:
    """Configure durable logs with IDs that can be matched to provider traces."""
    logger.remove()
    logger.add(sys.stderr, level=FRIDAY_LOG_LEVEL, format=_log_format)
    FRIDAY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        FRIDAY_LOG_FILE,
        level=FRIDAY_LOG_LEVEL,
        format=_log_format,
        rotation=FRIDAY_LOG_ROTATION,
        retention=FRIDAY_LOG_RETENTION,
        enqueue=True,
    )


configure_logging()


@asynccontextmanager
async def default_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    registry = ProviderRegistry()

    # ── OpenCode ───────────────────────────────────────────────────────
    base_url = os.environ.get("OPENCODE_BASE_URL", DEFAULT_OPENCODE_BASE_URL)
    opencode = OpencodeProvider(base_url)
    try:
        await asyncio.wait_for(opencode.start(), timeout=_OPENCODE_CONNECT_TIMEOUT)
        registry.add(opencode)
        logger.info("opencode provider started | url={}", base_url)
    except (TimeoutError, Exception) as err:
        logger.warning("opencode provider unavailable, skipping | err={}", err)
        await opencode.aclose()
        opencode = None  # type: ignore[assignment]

    # ── ClaudeCode ────────────────────────────────────────────────────
    # Always register — no external connection needed at startup. The SDK
    # reads sessions from disk and only hits the Anthropic API on send_turn.
    # Missing ANTHROPIC_API_KEY surfaces as an error when the first turn fires,
    # not as a missing harness option.
    claude = ClaudeCodeProvider()
    registry.add(claude)
    logger.info("claude-code provider started")

    # ── Codex ───────────────────────────────────────────────────────
    # Always register — codex CLI must be installed and in PATH.
    codex = CodexProvider()
    registry.add(codex)
    logger.info("codex provider started")

    if not registry.all():
        raise RuntimeError("no providers available — set ANTHROPIC_API_KEY or start opencode")

    app.state.registry = registry
    try:
        yield
    finally:
        await voice_shutdown()
        if opencode is not None:
            await opencode.aclose()
        await claude.aclose()
        await codex.aclose()


def _resolve_cors_origins() -> list[str]:
    raw = os.environ.get("FRIDAY_CORS_ORIGINS")
    if raw is None:
        return DEFAULT_CORS_ORIGINS
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    """Build the FastAPI app. Tests pass ``with_lifespan=False`` and inject
    a ``ProviderRegistry`` via ``app.dependency_overrides[get_registry]``.
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
    app.include_router(harnesses_router)
    app.include_router(voice_router)
    app.include_router(config_router)
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
