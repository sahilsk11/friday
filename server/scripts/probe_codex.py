"""End-to-end smoke test for CodexProvider + CodexSession.

Run against a live codex CLI::

    uv run python scripts/probe_codex.py

Creates a session, sends a short prompt, prints streaming text deltas as they
arrive, and reports the final accumulated text once the assistant turn ends.
"""

from __future__ import annotations

import asyncio
import sys
import time

from friday.core.codex_provider import CodexProvider
from friday.core.state import AgentState

PROMPT = "Reply with exactly: THE ANSWER IS FOUR"
TIMEOUT_S = 60.0


async def main() -> int:
    print(f"[probe] testing Codex provider")
    final_text: list[str] = []
    done = asyncio.Event()
    started_at = time.monotonic()

    provider = CodexProvider()
    session = await provider.create_session(title="probe")
    print(f"[probe] session_id={session.id}")

    async def on_delta(text: str) -> None:
        print(f"[delta] {text!r}")

    async def on_final(text: str) -> None:
        final_text.append(text)
        print(f"[probe] final text received ({len(text)} chars)")
        done.set()

    async def on_state(state: AgentState) -> None:
        print(f"[probe] state→{state.value}  (+{time.monotonic() - started_at:.2f}s)")

    async def on_tool_start(name: str, input: dict) -> None:
        print(f"[probe] tool_start: {name} with input={input}")

    session.on_text_delta(on_delta)
    session.on_text_final(on_final)
    session.on_state(on_state)
    session.on_tool_start(on_tool_start)

    print(f"[probe] sending prompt: {PROMPT!r}")
    await session.send_turn(PROMPT)

    try:
        await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)
    except TimeoutError:
        print(f"[probe] TIMEOUT after {TIMEOUT_S}s — cancelling")
        await session.cancel()
        await provider.aclose()
        return 1

    if not final_text:
        print("[probe] FAIL: no final text accumulated")
        await provider.aclose()
        return 1
    print(f"[probe] PASS — assistant said: {final_text[0]!r}")

    await provider.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))