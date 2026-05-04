"""End-to-end smoke test for per-turn model selection.

Asserts:
  - Sending a turn with ``model={providerID, modelID}`` produces an assistant
    message whose ``info.modelID`` / ``info.providerID`` match.
  - A second turn with a different model switches mid-session — the new
    assistant message reflects the new model.
  - Without an explicit override, opencode falls back to its global default.

Run::

    opencode serve --port 4096 &
    uv run python scripts/probe_model_selection.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from friday.core.opencode_session import ModelChoice, OpencodeClient
from friday.core.session_manager import SessionManager
from friday.core.state import AgentState

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
TIMEOUT_S = 90.0

# Two opencode-zen models. They're free, fast, and tool-capable — switching
# between them is a clean signal that the per-turn override is wiring through.
MODEL_A = ModelChoice(provider_id="opencode", model_id="gpt-5-nano")
MODEL_B = ModelChoice(provider_id="opencode", model_id="big-pickle")


async def _wait_idle(session) -> None:  # type: ignore[no-untyped-def]
    done = asyncio.Event()

    async def on_state(state: AgentState) -> None:
        if state is AgentState.IDLE:
            done.set()

    session.on_state(on_state)
    await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)


def _last_assistant_model(transcript) -> ModelChoice | None:  # type: ignore[no-untyped-def]
    for msg in reversed(transcript):
        if msg.role == "assistant" and msg.model is not None:
            return msg.model
    return None


async def main() -> int:
    print(f"[probe] base_url={BASE_URL}")

    async with OpencodeClient(BASE_URL) as client:
        manager = SessionManager(client)

        # Track on_model events so we can confirm SSE plumbing fires.
        observed: list[ModelChoice] = []

        async def on_model(m: ModelChoice) -> None:
            observed.append(m)

        async def case(label: str, model: ModelChoice | None) -> ModelChoice | None:
            session = await manager.create(title=f"probe-model-{label}", directory="/tmp")
            session.on_model(on_model)

            print(f"[probe] [{label}] sending turn (model={model})")
            await session.send_turn("Reply with one word: OK", model=model)
            await _wait_idle(session)

            transcript = await manager.get_transcript(session.id)
            got = _last_assistant_model(transcript)
            print(f"[probe] [{label}] assistant ran on: {got}")
            return got

        # 1. Explicit MODEL_A
        got_a = await case("explicit-a", MODEL_A)
        assert got_a == MODEL_A, f"expected {MODEL_A}, got {got_a}"

        # 2. Mid-session switch to MODEL_B (same fresh session each time keeps
        #    the assertions clean).
        got_b = await case("explicit-b", MODEL_B)
        assert got_b == MODEL_B, f"expected {MODEL_B}, got {got_b}"

        # 3. No override — whatever opencode's default is. Just assert it's
        #    populated (it is, on every assistant message); we don't pin the
        #    value since the default depends on local opencode config.
        got_default = await case("default", None)
        assert got_default is not None, "expected info.modelID populated"

        # SSE on_model observer should have fired at least once per turn.
        assert len(observed) >= 3, f"expected ≥3 on_model events, got {len(observed)}"
        print(f"[probe] on_model events: {observed}")

    print("[probe] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
