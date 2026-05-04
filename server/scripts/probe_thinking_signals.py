"""Timing probe for the signals that drive a "thinking" indicator.

Sends one prompt to opencode and reports the wall-clock timeline of:
  - state transitions (busy → idle)
  - first / last reasoning delta (if any)
  - first / last real text delta
  - tool starts
  - message.updated (turn complete)

Goal: design the UI from measurements, not guesses. Specifically:
  - Latency from prompt → state=busy (does the pulse have lag?)
  - Latency from state=busy → first content (reasoning or text)
  - Density of reasoning deltas (deltas/sec)
  - Longest dead-air window inside a turn (the case the pulse alone covers)
  - Whether state=idle is reliable (we've been bitten before — see
    opencode_session.py docstring on MessageUpdated being the real signal)

Usage::

    uv run python scripts/probe_thinking_signals.py
    PROBE_PROMPT="think step by step: is 17 prime?" uv run python scripts/probe_thinking_signals.py
    OPENCODE_BASE_URL=http://remote:4096 uv run python scripts/probe_thinking_signals.py

Like probe_raw_sse, this is diagnostic only — no TTS, no session reuse.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field

import httpx
from httpx_sse import aconnect_sse

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
PROMPT = os.environ.get(
    "PROBE_PROMPT",
    "Reply in exactly three words. No punctuation.",
)
TIMEOUT_S = float(os.environ.get("PROBE_TIMEOUT_S", "180"))


@dataclass
class Timeline:
    prompt_at: float
    first_busy_at: float | None = None
    last_idle_at: float | None = None
    first_reasoning_at: float | None = None
    last_reasoning_at: float | None = None
    first_text_at: float | None = None
    last_text_at: float | None = None
    message_complete_at: float | None = None
    reasoning_deltas: int = 0
    text_deltas: int = 0
    tool_starts: list[tuple[float, str]] = field(default_factory=list)
    # All content-producing event timestamps (any reasoning or text delta), in
    # order. Used to compute the longest dead-air window inside a busy state.
    content_event_times: list[float] = field(default_factory=list)

    def at(self, t: float) -> float:
        return t - self.prompt_at


def _format_dt(t: float | None, base: float) -> str:
    if t is None:
        return "  (none)"
    return f"+{t - base:6.2f}s"


async def _consume(
    http: httpx.AsyncClient,
    session_id: str,
    timeline: Timeline,
    done: asyncio.Event,
) -> None:
    # Map part_id → declared part type, so we can label text-field deltas as
    # reasoning vs real text. (Opencode emits reasoning deltas with field="text"
    # and you can only tell from the part's prior type declaration.)
    part_types: dict[str, str] = {}

    async with aconnect_sse(http, "GET", "/global/event") as source:
        # Give the SSE stream a moment to attach before sending the prompt.
        await asyncio.sleep(0.2)
        timeline.prompt_at = time.monotonic()
        pr = await http.post(
            f"/session/{session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": PROMPT}]},
        )
        pr.raise_for_status()
        print(f"[probe] prompt sent at t=0.00s, listening...\n")

        async for sse in source.aiter_sse():
            if not sse.data:
                continue
            try:
                raw = json.loads(sse.data)
            except json.JSONDecodeError:
                continue
            payload = raw.get("payload", raw)
            event_type = payload.get("type", "?")
            props = payload.get("properties") or {}
            evt_session = props.get("sessionID") or (props.get("info") or {}).get("sessionID")
            if evt_session and evt_session != session_id:
                continue

            now = time.monotonic()
            rel = timeline.at(now)

            if event_type == "session.status":
                # Wire shape: ``status: {type: "busy" | "idle" | ...}`` —
                # see events.py::_parse_session_status. The literal value
                # lives one level down from the props key.
                status = (props.get("status") or {}).get("type", "?")
                print(f"[+{rel:6.2f}s]  session.status -> {status!r}")
                if status == "busy" and timeline.first_busy_at is None:
                    timeline.first_busy_at = now
                if status == "idle":
                    timeline.last_idle_at = now

            elif event_type == "session.idle":
                print(f"[+{rel:6.2f}s]  session.idle")
                timeline.last_idle_at = now
                done.set()
                return

            elif event_type == "message.part.updated":
                part = props.get("part") or {}
                ptype = part.get("type", "?")
                pid = part.get("id", "")
                if pid:
                    part_types[pid] = ptype
                if ptype == "tool":
                    state = part.get("state") or {}
                    if state.get("status") == "running":
                        name = part.get("tool", "?")
                        print(f"[+{rel:6.2f}s]  tool.start  {name!r}")
                        timeline.tool_starts.append((now, name))

            elif event_type == "message.part.delta":
                field_name = props.get("field", "?")
                pid = props.get("partID", "")
                actual_type = part_types.get(pid, "?")
                if field_name != "text":
                    continue
                if actual_type == "reasoning":
                    timeline.reasoning_deltas += 1
                    if timeline.first_reasoning_at is None:
                        timeline.first_reasoning_at = now
                        delta_preview = (props.get("delta") or "")[:40]
                        print(
                            f"[+{rel:6.2f}s]  reasoning.first  {delta_preview!r}"
                        )
                    timeline.last_reasoning_at = now
                    timeline.content_event_times.append(now)
                else:
                    timeline.text_deltas += 1
                    if timeline.first_text_at is None:
                        timeline.first_text_at = now
                        delta_preview = (props.get("delta") or "")[:40]
                        print(f"[+{rel:6.2f}s]  text.first       {delta_preview!r}")
                    timeline.last_text_at = now
                    timeline.content_event_times.append(now)

            elif event_type == "message.updated":
                info = props.get("info") or {}
                t = info.get("time") or {}
                if info.get("role") == "assistant" and (t.get("completed") or t.get("end")):
                    timeline.message_complete_at = now
                    print(f"[+{rel:6.2f}s]  message.complete (assistant)")
                    # Don't return yet — opencode may still emit session.idle
                    # after this. Let the next loop iteration catch it.


def _print_summary(t: Timeline) -> None:
    base = t.prompt_at
    print()
    print("=" * 60)
    print("TIMELINE")
    print(f"  prompt sent           {_format_dt(base, base)}")
    print(f"  first state=busy      {_format_dt(t.first_busy_at, base)}")
    print(f"  first reasoning delta {_format_dt(t.first_reasoning_at, base)}")
    print(f"  first text delta      {_format_dt(t.first_text_at, base)}")
    print(f"  message.complete      {_format_dt(t.message_complete_at, base)}")
    print(f"  last state=idle       {_format_dt(t.last_idle_at, base)}")

    print()
    print("DELTAS")
    print(f"  reasoning deltas:  {t.reasoning_deltas}")
    print(f"  text deltas:       {t.text_deltas}")
    print(f"  tool starts:       {len(t.tool_starts)}")

    if t.first_reasoning_at and t.last_reasoning_at and t.reasoning_deltas > 1:
        span = t.last_reasoning_at - t.first_reasoning_at
        rate = t.reasoning_deltas / span if span > 0 else float("inf")
        print(f"  reasoning rate:    {rate:.1f} deltas/sec across {span:.2f}s")

    if t.first_text_at and t.last_text_at and t.text_deltas > 1:
        span = t.last_text_at - t.first_text_at
        rate = t.text_deltas / span if span > 0 else float("inf")
        print(f"  text rate:         {rate:.1f} deltas/sec across {span:.2f}s")

    # Longest gap between two consecutive content events. Bounds the case
    # where the indicator is the *only* signal of life.
    print()
    print("DEAD-AIR WINDOWS (max gap between content events while busy)")
    if len(t.content_event_times) < 2:
        print("  not enough content events to compute")
    else:
        sorted_times = sorted(t.content_event_times)
        gaps = [
            (sorted_times[i + 1] - sorted_times[i], sorted_times[i] - base)
            for i in range(len(sorted_times) - 1)
        ]
        gaps.sort(reverse=True)
        print(f"  largest:  {gaps[0][0]:.2f}s   (starting at +{gaps[0][1]:.2f}s)")
        if len(gaps) >= 3:
            print(f"  second:   {gaps[1][0]:.2f}s   (starting at +{gaps[1][1]:.2f}s)")
            print(f"  third:    {gaps[2][0]:.2f}s   (starting at +{gaps[2][1]:.2f}s)")
        # Also: gap from busy → first content (initial pulse window).
        if t.first_busy_at is not None:
            first_content = sorted_times[0]
            initial = first_content - t.first_busy_at
            print(
                f"  initial:  {initial:.2f}s   (busy → first content; pure pulse window)"
            )

    print()
    print("VERDICT")
    if t.reasoning_deltas == 0:
        print("  no reasoning deltas observed — option B (reasoning stream)")
        print("  won't show anything for this prompt/model. Pulse-only it is.")
    else:
        print(f"  reasoning streams ARE available ({t.reasoning_deltas} deltas).")
        print("  option B (dimmed reasoning in feed) will work end-to-end.")
    if t.first_busy_at is None:
        print("  ⚠  never saw state=busy — UI pulse won't activate!")
    if t.last_idle_at is None and t.message_complete_at is None:
        print("  ⚠  never saw idle/complete — pulse would stick on forever!")


async def main() -> int:
    print(f"[probe] base_url={BASE_URL}")
    print(f"[probe] prompt={PROMPT!r}")
    print(f"[probe] timeout={TIMEOUT_S}s")
    print()

    done = asyncio.Event()
    timeline = Timeline(prompt_at=0.0)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as http:
        resp = await http.post("/session", json={"title": "thinking-probe"})
        resp.raise_for_status()
        session_id: str = resp.json()["id"]
        print(f"[probe] session_id={session_id}\n")

        consume_task = asyncio.create_task(_consume(http, session_id, timeline, done))
        try:
            await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)
        except TimeoutError:
            print(f"[probe] TIMEOUT after {TIMEOUT_S}s")
            consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await consume_task
            _print_summary(timeline)
            return 1

        consume_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await consume_task

    _print_summary(timeline)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
