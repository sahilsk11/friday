"""End-to-end smoke test for OpencodeClient + OpencodeSession.

Run against a live opencode server::

    opencode serve --port 4096 &
    uv run python scripts/probe_opencode.py

Creates a session, sends a short prompt, prints streaming text deltas as they
arrive, and reports the final accumulated text once the assistant turn ends.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from friday.core.opencode_session import OpencodeClient
from friday.core.state import AgentState

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
PROMPT = "Reply with exactly: HELLO FRIDAY (uppercase, no punctuation)"
TIMEOUT_S = 60.0


async def main() -> int:
    print(f"[probe] base_url={BASE_URL}")
    final_text: list[str] = []
    done = asyncio.Event()
    started_at = time.monotonic()

    async with OpencodeClient(BASE_URL) as client:
        session = await client.new_session(title="probe")
        print(f"[probe] session_id={session.id}")

        async def on_delta(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

        async def on_final(text: str) -> None:
            final_text.append(text)
            print(f"\n[probe] final ({len(text)} chars)")
            done.set()

        async def on_state(state: AgentState) -> None:
            print(f"\n[probe] state→{state.value}  (+{time.monotonic() - started_at:.2f}s)")

        session.on_text_delta(on_delta)
        session.on_text_final(on_final)
        session.on_state(on_state)

        print(f"[probe] sending prompt: {PROMPT!r}")
        await session.send_turn(PROMPT)

        try:
            await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)
        except TimeoutError:
            print(f"[probe] TIMEOUT after {TIMEOUT_S}s — cancelling")
            await session.cancel()
            return 1

    if not final_text:
        print("[probe] FAIL: no final text accumulated")
        return 1
    print(f"[probe] PASS — assistant said: {final_text[0]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
