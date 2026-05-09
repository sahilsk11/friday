"""API for client configuration."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/config", tags=["config"])


class ClientConfig(BaseModel):
    defaultDirectory: str  # noqa: N815


def _load_config() -> ClientConfig:
    """Load config from file, with fallbacks."""
    config_paths = [
        Path.home() / ".config" / "friday" / "config.json",
        Path.home() / "projects" / ".friday-config.json",
    ]

    for path in config_paths:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                    if "defaultDirectory" in data:
                        return ClientConfig(defaultDirectory=data["defaultDirectory"])
            except (json.JSONDecodeError, OSError):
                pass

    return ClientConfig(defaultDirectory="/home/sas/projects")


@router.get("", response_model=ClientConfig)
async def get_config() -> ClientConfig:
    """Return client-facing configuration."""
    return _load_config()