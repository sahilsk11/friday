"""Throwaway: exercise everything built in Steps 1 + 2 against live opencode.

Covers:
1. ``list_sessions`` baseline
2. ``create`` returns a live OpencodeSession registered in the cache
3. ``attach`` (via the cache) returns the *same* instance, so observers stick
4. Streaming text deltas + state transitions during a turn
5. Sequential queueing: two prompts back-to-back drain in order
6. ``get_transcript`` reflects both turns with completed_at timestamps
7. ``list_sessions(directory=...)`` filter narrows to the new session
8. Idle-state handler is invoked and is idempotent across opencode's
   duplicate terminal events
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

from friday.core.opencode_session import OpencodeClient, OpencodeSession
from friday.core.session_manager import SessionManager
from friday.core.state import AgentState

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
TURN_TIMEOUT_S = 90.0


@dataclass
class TurnRecorder:
    """Captures the lifecycle of a single assistant turn for assertions."""

    label: str
    deltas: list[str] = field(default_factory=list)
    final_text: str | None = None
    states: list[AgentState] = field(default_factory=list)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def attach(self, session: OpencodeSession) -> None:
        async def on_delta(text: str) -> None:
            self.deltas.append(text)

        async def on_final(text: str) -> None:
            self.final_text = text

        async def on_state(state: AgentState) -> None:
            self.states.append(state)
            if state is AgentState.IDLE and self.final_text is not None:
                self.finished.set()

        session.on_text_delta(on_delta)
        session.on_text_final(on_final)
        session.on_state(on_state)


def fail(msg: str) -> int:
    print(f"[verify] FAIL — {msg}")
    return 1


async def main() -> int:
    print(f"[verify] base_url={BASE_URL}")
    cwd = os.getcwd()
    started = time.monotonic()

    async with OpencodeClient(BASE_URL) as client:
        manager = SessionManager(client)

        # 1. baseline list
        baseline = await manager.list_sessions()
        print(f"[verify] baseline: {len(baseline)} sessions")

        # 2. create → live wrapper
        session = await manager.create(title="verify-step2")
        print(f"[verify] created {session.id} (+{time.monotonic() - started:.2f}s)")

        # 3. attach returns the SAME instance (cache hit)
        same = manager.attach(session.id)
        if same is not session:
            return fail("attach() did not return the cached OpencodeSession")
        print("[verify] attach() returned cached instance ✓")

        # 4 + 8. first turn — record deltas + states
        turn1 = TurnRecorder(label="turn1")
        turn1.attach(session)

        print("[verify] sending turn 1: 'Reply with exactly: ALPHA'")
        await session.send_turn("Reply with exactly: ALPHA (uppercase, no punctuation)")

        # 5. queue turn 2 immediately — opencode should serialize them
        turn2 = TurnRecorder(label="turn2")
        # Reuse the same observer registration paths; both observers see all events
        # (they're per-session, not per-turn). We track them via the finished gate
        # which only flips after on_final fires.
        turn2.attach(session)

        await asyncio.sleep(0.05)  # nudge ordering — both POSTs happen before either finishes
        print("[verify] sending turn 2: 'Reply with exactly: BETA'")
        await session.send_turn("Reply with exactly: BETA (uppercase, no punctuation)")

        # Wait for both to drain. The recorders share the session, so each "final"
        # arrives once. We wait on turn1 first (arrives first), then turn2.
        try:
            await asyncio.wait_for(turn1.finished.wait(), timeout=TURN_TIMEOUT_S)
        except TimeoutError:
            return fail(f"turn1 did not finish within {TURN_TIMEOUT_S}s")
        print(
            f"[verify] turn1 final={turn1.final_text!r} states={[s.value for s in turn1.states]} "
            f"(+{time.monotonic() - started:.2f}s)"
        )

        # Reset the gate; turn2's on_final will fire next.
        turn2.finished.clear()
        # turn1's recorder also has finished set already; that's fine — we only wait turn2.
        try:
            await asyncio.wait_for(turn2.finished.wait(), timeout=TURN_TIMEOUT_S)
        except TimeoutError:
            return fail(f"turn2 did not finish within {TURN_TIMEOUT_S}s")
        print(
            f"[verify] turn2 final={turn2.final_text!r} states={[s.value for s in turn2.states]} "
            f"(+{time.monotonic() - started:.2f}s)"
        )

        # 6. transcript reflects both turns + completed_at populated
        transcript = await manager.get_transcript(session.id)
        roles = [m.role for m in transcript]
        if roles != ["user", "assistant", "user", "assistant"]:
            return fail(f"unexpected transcript role order: {roles}")
        if any(m.completed_at is None for m in transcript if m.role == "assistant"):
            return fail("at least one assistant message missing completed_at")
        if "ALPHA" not in transcript[1].text or "BETA" not in transcript[3].text:
            return fail(
                f"sequencing wrong: turn1={transcript[1].text!r} turn2={transcript[3].text!r}"
            )
        print("[verify] transcript ordering ✓ ALPHA → BETA")

        # 7. directory filter narrows correctly
        # opencode records the cwd of the friday process at session creation.
        narrowed = await manager.list_sessions(directory=cwd)
        if not any(s.id == session.id for s in narrowed):
            return fail(f"directory filter did not include new session for cwd={cwd}")
        outside = await manager.list_sessions(directory="/nonexistent/dir/that/should/not/match")
        if outside:
            return fail(f"directory filter let through {len(outside)} unrelated sessions")
        print(f"[verify] directory filter ✓ ({len(narrowed)} sessions match cwd)")

        # 8. duplicate idle states — recorders captured every state event
        # opencode emits THINKING then IDLE per turn; we expect the IDLE count to
        # be ≥ number of turns (duplicates are tolerated, not required).
        idle_count_t1 = sum(1 for s in turn1.states if s is AgentState.IDLE)
        idle_count_t2 = sum(1 for s in turn2.states if s is AgentState.IDLE)
        thinking_count = sum(1 for s in turn1.states if s is AgentState.THINKING)
        if idle_count_t1 < 1 or idle_count_t2 < 1:
            return fail(
                f"missing IDLE states: turn1={idle_count_t1} turn2={idle_count_t2}"
            )
        if thinking_count < 1:
            return fail(f"never saw THINKING state: states={turn1.states}")
        print(
            f"[verify] state events ✓ "
            f"(thinking≥1, idle counts t1={idle_count_t1} t2={idle_count_t2})"
        )

        # And turn1 had streaming deltas at some point.
        if not turn1.deltas:
            return fail("no text deltas streamed during turn1")
        if turn1.final_text is None or "ALPHA" not in turn1.final_text:
            return fail(f"turn1 final missing ALPHA: {turn1.final_text!r}")
        print(f"[verify] streamed {len(turn1.deltas)} delta(s) for turn1 ✓")

    print(f"[verify] ALL PASS (+{time.monotonic() - started:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
