from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from friday.core import codex_provider
from friday.core.codex_provider import CodexProvider, CodexSession


async def test_codex_thread_started_assigns_session_id_and_notifies_callback() -> None:
    session = CodexSession()
    assigned: list[str] = []

    async def on_sdk_id(sdk_id: str) -> None:
        assigned.append(sdk_id)

    session._on_sdk_id_assigned = on_sdk_id  # pyright: ignore[reportPrivateUsage]

    await session._handle_event(  # pyright: ignore[reportPrivateUsage]
        {"type": "thread.started", "thread_id": "019e0f9a-6ea6-7831-ba7f-2791aa73d433"}
    )
    await asyncio.sleep(0)

    assert session.id == "019e0f9a-6ea6-7831-ba7f-2791aa73d433"
    assert assigned == ["019e0f9a-6ea6-7831-ba7f-2791aa73d433"]


async def test_codex_list_sessions_uses_real_jsonl_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "sessions" / "2026" / "05" / "10"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "codex-session-019e0f9a-6ea6-7831-ba7f-2791aa73d433.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-10T01:57:20.320Z",
                "type": "session_meta",
                "payload": {"id": "019e0f9a-6ea6-7831-ba7f-2791aa73d433"},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(codex_provider, "_CODEX_SESSION_DIR", tmp_path / "sessions")

    sessions = await CodexProvider().list_sessions()

    assert sessions[0].id == "019e0f9a-6ea6-7831-ba7f-2791aa73d433"
