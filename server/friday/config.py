"""Settings loaded from environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(_REPO_ROOT / ".env")

FRIDAY_LOG_LEVEL = os.environ.get("FRIDAY_LOG_LEVEL", "INFO").upper()
_FRIDAY_LOG_FILENAME = "friday.log"
FRIDAY_LOG_FILE = Path(
    os.environ.get("FRIDAY_LOG_FILE")
    or Path.cwd() / "logs" / _FRIDAY_LOG_FILENAME
)
FRIDAY_LOG_ROTATION = os.environ.get("FRIDAY_LOG_ROTATION", "10 MB")
FRIDAY_LOG_RETENTION = os.environ.get("FRIDAY_LOG_RETENTION", "7 days")
