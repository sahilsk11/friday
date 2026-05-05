#!/usr/bin/env python3
"""Probe script to test ClaudeCodeSession against the real Agent SDK."""

import asyncio
import sys

sys.path.insert(0, "/Users/sahil.kapur/Projects/friday/server")

from friday.core.claude_code_session import ClaudeCodeProvider, ClaudeCodeSession
from friday.core.state import AgentState


async def main():
    print("=> Probing ClaudeCodeProvider...")

    try:
        provider = ClaudeCodeProvider()
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 1

    print(f"  provider_id: {provider.provider_id}")

    print("=> Creating session...")
    session = await provider.create_session(title="probe-test")
    print(f"  session.id: {session.id}")
    print(f"  session.current_state: {session.current_state}")

    text_deltas = []
    text_final = []
    tool_starts = []
    state_changes = []

    # Must be async functions
    async def on_delta(d: str):
        text_deltas.append(d)

    async def on_final(t: str):
        text_final.append(t)

    async def on_state(s: AgentState):
        state_changes.append(s)

    async def on_tool(name: str, inp: dict):
        tool_starts.append((name, inp))

    session.on_text_delta(on_delta)
    session.on_text_final(on_final)
    session.on_state(on_state)
    session.on_tool_start(on_tool)

    print("=> Sending turn...")
    await session.send_turn(
        "List the files in the current directory.",
        system="You are a helpful assistant."
    )

    print("  Waiting for response...")
    try:
        await asyncio.wait_for(session._query_task, timeout=120)
    except asyncio.TimeoutError:
        print("  TIMEOUT waiting for response")
        await session.cancel()
    except asyncio.CancelledError:
        print("  Task cancelled")

    print(f"  Final state: {session.current_state}")
    print(f"  session.id (post-turn): {session.id!r}")
    print(f"  Text deltas received: {len(text_deltas)}")
    if text_deltas:
        print(f"  First delta: {text_deltas[0][:100]}...")
    print(f"  Text final received: {len(text_final)}")
    if text_final:
        print(f"  Final text: {text_final[0][:200]}...")
    print(f"  Tool starts: {tool_starts}")
    print(f"  State changes: {state_changes}")

    if text_final:
        print("\nSUCCESS: Got final text")
        return 0
    else:
        print("\nFAIL: No final text received")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))