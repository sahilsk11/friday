"""Compatibility shim for SQLite narrator persistence."""

from __future__ import annotations

from friday.infra.persistence.sqlite_narrator_store import NarratorStore

__all__ = ["NarratorStore"]
