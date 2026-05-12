from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from friday.domain.repositories import (
    NarratorRepository,
    StoredNarratorEvent,
    StoredNarratorMessage,
    StoredProviderEvent,
    StoredSession,
    StoredTurn,
)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _to_db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _from_db_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class NarratorStore(NarratorRepository):
    """SQLite persistence for Friday session and narrator state.

    The store persists Friday's voice/narrator memory. Provider-native coding
    transcripts remain provider-owned and are linked through provider_session_id.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        self._conn = conn
        self._migrate()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def upsert_session(
        self,
        *,
        session_id: str,
        provider_session_id: str,
        harness: str,
        model_id: str | None,
        title: str | None,
        directory: str | None,
    ) -> StoredSession:
        now = utc_now()
        with self._lock:
            conn = self._require_conn()
            existing = self.get_session(session_id)
            created_at = existing.created_at if existing else now
            conn.execute(
                """
                INSERT INTO sessions (
                    id, provider_session_id, harness, model_id, title, directory,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider_session_id = excluded.provider_session_id,
                    harness = excluded.harness,
                    model_id = excluded.model_id,
                    title = COALESCE(excluded.title, sessions.title),
                    directory = COALESCE(excluded.directory, sessions.directory),
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    provider_session_id,
                    harness,
                    model_id,
                    title,
                    directory,
                    _to_db_time(created_at),
                    _to_db_time(now),
                ),
            )
            conn.commit()
            session = self.get_session(session_id)
            assert session is not None
            return session

    def get_session(self, session_id: str) -> StoredSession | None:
        with self._lock:
            row = (
                self._require_conn()
                .execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                )
                .fetchone()
            )
        return _parse_session(row) if row is not None else None

    def list_sessions(self) -> list[StoredSession]:
        with self._lock:
            rows = (
                self._require_conn()
                .execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 100")
                .fetchall()
            )
        return [_parse_session(row) for row in rows]

    def update_session_provider_session_id(
        self,
        *,
        session_id: str,
        provider_session_id: str,
    ) -> StoredSession:
        now = utc_now()
        with self._lock:
            conn = self._require_conn()
            existing = self.get_session(session_id)
            if existing is None:
                raise KeyError(session_id)
            conn.execute(
                """
                UPDATE sessions
                SET provider_session_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (provider_session_id, _to_db_time(now), session_id),
            )
            conn.execute(
                """
                UPDATE provider_events
                SET provider_session_id = ?
                WHERE session_id = ? AND provider_session_id = ?
                """,
                (provider_session_id, session_id, existing.provider_session_id),
            )
            conn.execute(
                """
                UPDATE narrator_turns
                SET provider_session_id = ?
                WHERE session_id = ? AND provider_session_id = ?
                """,
                (provider_session_id, session_id, existing.provider_session_id),
            )
            conn.commit()
            session = self.get_session(session_id)
            assert session is not None
            return session

    def update_session_title(
        self,
        *,
        session_id: str,
        title: str | None,
    ) -> StoredSession:
        now = utc_now()
        normalized = title.strip() if isinstance(title, str) else None
        if normalized == "":
            normalized = None
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized, _to_db_time(now), session_id),
            )
            conn.commit()
            session = self.get_session(session_id)
            if session is None:
                raise KeyError(session_id)
            return session

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        source: str,
    ) -> None:
        now = utc_now()
        with self._lock:
            self._require_conn().execute(
                """
                INSERT INTO narrator_messages (session_id, role, content, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, content, source, _to_db_time(now)),
            )
            self._touch_session(session_id, now)
            self._require_conn().commit()

    def list_messages(self, session_id: str) -> list[StoredNarratorMessage]:
        with self._lock:
            rows = (
                self._require_conn()
                .execute(
                    """
                SELECT * FROM narrator_messages
                WHERE session_id = ?
                ORDER BY id
                """,
                    (session_id,),
                )
                .fetchall()
            )
        return [_parse_narrator_message(row) for row in rows]

    def append_narrator_event(
        self,
        *,
        session_id: str,
        event_type: str,
        text: str | None = None,
        payload: dict[str, Any] | None = None,
        event_key: str | None = None,
    ) -> StoredNarratorEvent:
        now = utc_now()
        payload_json = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            conn = self._require_conn()
            existing = (
                self.get_narrator_event_by_key(session_id=session_id, event_key=event_key)
                if event_key is not None
                else None
            )
            if existing is not None:
                return existing
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO narrator_events (
                        session_id, type, text, payload_json, created_at, event_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        event_type,
                        text,
                        payload_json,
                        _to_db_time(now),
                        event_key,
                    ),
                )
            except sqlite3.IntegrityError:
                if event_key is None:
                    raise
                existing = self.get_narrator_event_by_key(
                    session_id=session_id,
                    event_key=event_key,
                )
                if existing is None:
                    raise
                return existing
            self._touch_session(session_id, now)
            conn.commit()
            lastrowid = cursor.lastrowid
            assert lastrowid is not None
            event_id = int(lastrowid)
        return StoredNarratorEvent(
            id=event_id,
            session_id=session_id,
            type=event_type,
            text=text,
            payload=payload or {},
            created_at=now,
        )

    def get_narrator_event_by_key(
        self,
        *,
        session_id: str,
        event_key: str | None,
    ) -> StoredNarratorEvent | None:
        if event_key is None:
            return None
        with self._lock:
            row = (
                self._require_conn()
                .execute(
                    """
                SELECT * FROM narrator_events
                WHERE session_id = ? AND event_key = ?
                """,
                    (session_id, event_key),
                )
                .fetchone()
            )
        return _parse_narrator_event(row) if row is not None else None

    def append_provider_event(
        self,
        *,
        session_id: str,
        provider_session_id: str,
        event_type: str,
        summary: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> StoredProviderEvent:
        now = utc_now()
        payload_json = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        with self._lock:
            conn = self._require_conn()
            cursor = conn.execute(
                """
                INSERT INTO provider_events (
                    session_id, provider_session_id, type, summary, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    provider_session_id,
                    event_type,
                    summary,
                    payload_json,
                    _to_db_time(now),
                ),
            )
            self._touch_session(session_id, now)
            conn.commit()
            lastrowid = cursor.lastrowid
            assert lastrowid is not None
            event_id = int(lastrowid)
        return StoredProviderEvent(
            id=event_id,
            session_id=session_id,
            provider_session_id=provider_session_id,
            type=event_type,
            summary=summary,
            payload=payload or {},
            created_at=now,
        )

    def list_narrator_events(
        self,
        *,
        session_id: str,
        after_id: int = 0,
        limit: int = 50,
    ) -> list[StoredNarratorEvent]:
        with self._lock:
            rows = (
                self._require_conn()
                .execute(
                    """
                SELECT * FROM narrator_events
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                    (session_id, after_id, limit),
                )
                .fetchall()
            )
        return [_parse_narrator_event(row) for row in rows]

    def list_provider_events(
        self,
        *,
        session_id: str,
        limit: int = 10,
    ) -> list[StoredProviderEvent]:
        with self._lock:
            rows = (
                self._require_conn()
                .execute(
                    """
                SELECT * FROM provider_events
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                    (session_id, limit),
                )
                .fetchall()
            )
        return list(reversed([_parse_provider_event(row) for row in rows]))

    def create_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        provider_session_id: str,
        user_text: str,
        source: str,
    ) -> StoredTurn:
        now = utc_now()
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                """
                INSERT INTO narrator_turns (
                    id, session_id, provider_session_id, user_text, source, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    provider_session_id,
                    user_text,
                    source,
                    "submitted",
                    _to_db_time(now),
                    _to_db_time(now),
                ),
            )
            self._touch_session(session_id, now)
            conn.commit()
        turn = self.get_turn(turn_id)
        assert turn is not None
        return turn

    def get_turn(self, turn_id: str) -> StoredTurn | None:
        with self._lock:
            row = (
                self._require_conn()
                .execute(
                    "SELECT * FROM narrator_turns WHERE id = ?",
                    (turn_id,),
                )
                .fetchone()
            )
        return _parse_turn(row) if row is not None else None

    def latest_turn(self, session_id: str) -> StoredTurn | None:
        with self._lock:
            row = (
                self._require_conn()
                .execute(
                    """
                SELECT * FROM narrator_turns
                WHERE session_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                    (session_id,),
                )
                .fetchone()
            )
        return _parse_turn(row) if row is not None else None

    def latest_active_turn(
        self,
        *,
        session_id: str,
        provider_session_id: str,
    ) -> StoredTurn | None:
        with self._lock:
            row = (
                self._require_conn()
                .execute(
                    """
                SELECT * FROM narrator_turns
                WHERE session_id = ?
                  AND provider_session_id = ?
                  AND status NOT IN ('completed', 'cancelled', 'error')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                    (session_id, provider_session_id),
                )
                .fetchone()
            )
        return _parse_turn(row) if row is not None else None

    def update_turn_status(
        self,
        *,
        turn_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        completed_at = now if status in {"completed", "cancelled", "error"} else None
        with self._lock:
            self._require_conn().execute(
                """
                UPDATE narrator_turns
                SET status = ?, error = COALESCE(?, error), updated_at = ?,
                    completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (
                    status,
                    error,
                    _to_db_time(now),
                    _to_db_time(completed_at) if completed_at is not None else None,
                    turn_id,
                ),
            )
            self._require_conn().commit()

    def mark_turn_provider_final(
        self,
        *,
        turn_id: str,
        provider_final_text: str,
        provider_final_event_id: int,
    ) -> None:
        now = utc_now()
        with self._lock:
            self._require_conn().execute(
                """
                UPDATE narrator_turns
                SET status = ?, provider_final_text = ?, provider_final_event_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    "provider_final",
                    provider_final_text,
                    provider_final_event_id,
                    _to_db_time(now),
                    turn_id,
                ),
            )
            self._require_conn().commit()

    def mark_turn_completed(
        self,
        *,
        turn_id: str,
        narrator_final_text: str,
        narrator_final_event_id: int,
    ) -> None:
        now = utc_now()
        with self._lock:
            self._require_conn().execute(
                """
                UPDATE narrator_turns
                SET status = ?, narrator_final_text = ?, narrator_final_event_id = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    "completed",
                    narrator_final_text,
                    narrator_final_event_id,
                    _to_db_time(now),
                    _to_db_time(now),
                    turn_id,
                ),
            )
            self._require_conn().commit()

    def recoverable_turns(
        self,
        *,
        session_id: str,
        min_provider_final_age_seconds: float,
        limit: int = 5,
    ) -> list[StoredTurn]:
        cutoff = utc_now().timestamp() - min_provider_final_age_seconds
        with self._lock:
            rows = (
                self._require_conn()
                .execute(
                    """
                SELECT * FROM narrator_turns
                WHERE session_id = ?
                  AND provider_final_text IS NOT NULL
                  AND narrator_final_event_id IS NULL
                  AND status NOT IN ('completed', 'cancelled', 'error')
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                    (session_id, limit),
                )
                .fetchall()
            )
        turns = [_parse_turn(row) for row in rows]
        return [turn for turn in turns if turn.updated_at.timestamp() <= cutoff]

    def _touch_session(self, session_id: str, now: datetime) -> None:
        self._require_conn().execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (_to_db_time(now), session_id),
        )

    def _migrate(self) -> None:
        conn = self._require_conn()
        with self._lock:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    provider_session_id TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    model_id TEXT,
                    title TEXT,
                    directory TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS narrator_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS narrator_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    text TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS provider_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    provider_session_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    summary TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS narrator_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    provider_session_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_final_text TEXT,
                    narrator_final_text TEXT,
                    provider_final_event_id INTEGER,
                    narrator_final_event_id INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_narrator_events_session_id_id
                    ON narrator_events(session_id, id);

                CREATE INDEX IF NOT EXISTS idx_provider_events_session_id_id
                    ON provider_events(session_id, id);

                CREATE INDEX IF NOT EXISTS idx_narrator_turns_session_status
                    ON narrator_turns(session_id, status, updated_at);
                """
            )
            self._ensure_column("narrator_events", "event_key", "TEXT")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_narrator_events_session_event_key
                ON narrator_events(session_id, event_key)
                WHERE event_key IS NOT NULL
                """
            )
            conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"]) for row in self._require_conn().execute(f"PRAGMA table_info({table})")
        }
        if column in columns:
            return
        self._require_conn().execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("NarratorStore has not been started")
        return self._conn


def _parse_session(row: sqlite3.Row) -> StoredSession:
    return StoredSession(
        id=str(row["id"]),
        provider_session_id=str(row["provider_session_id"]),
        harness=str(row["harness"]),
        model_id=row["model_id"],
        title=row["title"],
        directory=row["directory"],
        created_at=_from_db_time(str(row["created_at"])),
        updated_at=_from_db_time(str(row["updated_at"])),
    )


def _parse_narrator_message(row: sqlite3.Row) -> StoredNarratorMessage:
    return StoredNarratorMessage(
        id=int(row["id"]),
        session_id=str(row["session_id"]),
        role=str(row["role"]),
        content=str(row["content"]),
        source=str(row["source"]),
        created_at=_from_db_time(str(row["created_at"])),
    )


def _parse_narrator_event(row: sqlite3.Row) -> StoredNarratorEvent:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        payload = {}
    return StoredNarratorEvent(
        id=int(row["id"]),
        session_id=str(row["session_id"]),
        type=str(row["type"]),
        text=row["text"],
        payload=payload,
        created_at=_from_db_time(str(row["created_at"])),
    )


def _parse_provider_event(row: sqlite3.Row) -> StoredProviderEvent:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        payload = {}
    return StoredProviderEvent(
        id=int(row["id"]),
        session_id=str(row["session_id"]),
        provider_session_id=str(row["provider_session_id"]),
        type=str(row["type"]),
        summary=row["summary"],
        payload=payload,
        created_at=_from_db_time(str(row["created_at"])),
    )


def _parse_turn(row: sqlite3.Row) -> StoredTurn:
    completed_at = row["completed_at"]
    return StoredTurn(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        provider_session_id=str(row["provider_session_id"]),
        user_text=str(row["user_text"]),
        source=str(row["source"]),
        status=str(row["status"]),
        provider_final_text=row["provider_final_text"],
        narrator_final_text=row["narrator_final_text"],
        provider_final_event_id=row["provider_final_event_id"],
        narrator_final_event_id=row["narrator_final_event_id"],
        error=row["error"],
        created_at=_from_db_time(str(row["created_at"])),
        updated_at=_from_db_time(str(row["updated_at"])),
        completed_at=(_from_db_time(str(completed_at)) if completed_at is not None else None),
    )
