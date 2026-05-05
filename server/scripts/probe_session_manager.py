"""End-to-end smoke test for SessionManager against a live opencode server.

Run::

    opencode serve --port 4096 &
    uv run python scripts/probe_session_manager.py

Asserts: ``list_sessions`` returns rows, ``create`` registers a fresh wrapper,
``send_turn`` + SSE drives the assistant to completion, and ``get_transcript``
surfaces the user prompt + assistant reply with completed timestamps.
"""

from __future__ import annotations

import asyncio
import os
import sys

from friday.core.opencode_provider import OpencodeProvider
from friday.core.session_manager import SessionManager
from friday.core.state import AgentState

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
PROMPT = "Reply with exactly: HI (uppercase, no punctuation)"
TIMEOUT_S = 60.0


async def main() -> int:
    print(f"[probe] base_url={BASE_URL}")
    done = asyncio.Event()

    async with OpencodeProvider(BASE_URL) as client:
        manager = SessionManager(client)

        existing = await manager.list_sessions()
        print(f"[probe] listed {len(existing)} existing sessions")

        session = await manager.create(title="probe-session-manager")
        print(f"[probe] created session {session.id}")

        async def on_state(state: AgentState) -> None:
            if state is AgentState.IDLE:
                done.set()

        session.on_state(on_state)

        print(f"[probe] sent turn: {PROMPT!r}")
        await session.send_turn(PROMPT)

        try:
            await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)
        except TimeoutError:
            print(f"[probe] TIMEOUT after {TIMEOUT_S}s")
            await session.cancel()
            return 1

        info = await manager.get(session.id)
        print(f"[probe] get(): title={info.title!r} created_at={info.created_at.isoformat()}")

        transcript = await manager.get_transcript(session.id)
        print("[probe] transcript after completion:")
        for msg in transcript:
            stamp = msg.completed_at.isoformat() if msg.completed_at else "—"
            print(f"  [{msg.role:9}] ({stamp}) {msg.text!r}")

        if len(transcript) < 2:
            print(f"[probe] FAIL: expected ≥2 messages, got {len(transcript)}")
            return 1
        if transcript[0].role != "user" or transcript[1].role != "assistant":
            print(f"[probe] FAIL: unexpected roles: {[m.role for m in transcript]}")
            return 1
        if not transcript[1].text.strip():
            print("[probe] FAIL: assistant text empty")
            return 1
        if transcript[1].completed_at is None:
            print("[probe] FAIL: assistant completed_at not set")
            return 1

    print("[probe] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
