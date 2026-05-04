"""Raw SSE event dumper for opencode.

Connects to an opencode server, creates a session, sends a prompt, and
prints every raw SSE event as it arrives — with no filtering.  Use this
to understand what thinking/reasoning content looks like on the wire.

Usage::

    # Against local server (default)
    uv run python scripts/probe_raw_sse.py

    # Against production / remote server
    OPENCODE_BASE_URL=http://remote:4096 uv run python scripts/probe_raw_sse.py

    # With a specific prompt
    PROBE_PROMPT="think step by step: what is 17 * 23?" uv run python scripts/probe_raw_sse.py

The script prints:
  - Each raw SSE event line exactly as received
  - A parsed summary showing event type, part type, field, and delta preview
  - A final breakdown counting events by (type, part_type/field) to spot
    reasoning vs text deltas

This is a diagnostic tool only — no TTS or session management logic.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter
from typing import Any

import httpx
from httpx_sse import aconnect_sse

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
PROMPT = os.environ.get(
    "PROBE_PROMPT",
    "Reply in exactly three words. No punctuation.",
)
TIMEOUT_S = 120.0


def _summarize(payload: dict[str, Any]) -> str:
    """One-line human-readable summary of a parsed event payload."""
    event_type = payload.get("type", "?")
    props = payload.get("properties") or {}

    if event_type == "message.part.delta":
        field = props.get("field", "?")
        delta = props.get("delta", "")
        part_id = props.get("partID", "?")[:8]
        preview = repr(delta[:40]) if delta else "(empty)"
        return f"{event_type}  field={field!r}  part={part_id}  delta={preview}"

    if event_type == "message.part.updated":
        part = props.get("part") or {}
        ptype = part.get("type", "?")
        part_id = (part.get("id") or "?")[:8]
        delta = props.get("delta")
        extra = f"  inline_delta={repr(delta[:40])!r}" if delta else ""
        status = ""
        if ptype == "tool":
            state = part.get("state") or {}
            status = f"  tool_status={state.get('status', '?')!r}  tool={part.get('tool', '?')!r}"
        text_preview = ""
        if ptype in ("text", "reasoning"):
            txt = part.get("text", "")
            text_preview = f"  text_so_far={repr(txt[:60])}"
        return f"{event_type}  part_type={ptype!r}  part={part_id}{status}{text_preview}{extra}"

    if event_type == "message.updated":
        info = props.get("info") or {}
        model = info.get("model") or {}
        model_str = f"  model={model.get('providerID','?')}/{model.get('modelID','?')}" if model else ""
        return (
            f"{event_type}  role={info.get('role','?')!r}"
            f"  time_end={bool((info.get('time') or {}).get('completed') or (info.get('time') or {}).get('end'))}"
            f"{model_str}"
        )

    return f"{event_type}"


async def main() -> int:
    print(f"[raw-probe] base_url={BASE_URL}")
    print(f"[raw-probe] prompt={PROMPT!r}")
    print()

    event_counts: Counter[str] = Counter()
    done = asyncio.Event()
    session_id: str | None = None
    # Track part IDs by type so we can label text-field deltas correctly
    part_types: dict[str, str] = {}
    model_info: list[str] = []

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as http:
        # Create session
        resp = await http.post("/session", json={"title": "raw-probe"})
        resp.raise_for_status()
        session_id = resp.json()["id"]
        print(f"[raw-probe] session_id={session_id}")

        async def _consume() -> None:
            async with aconnect_sse(http, "GET", "/global/event") as source:
                # Give the SSE stream a moment, then send the prompt
                await asyncio.sleep(0.2)
                pr = await http.post(
                    f"/session/{session_id}/prompt_async",
                    json={"parts": [{"type": "text", "text": PROMPT}]},
                )
                pr.raise_for_status()
                print(f"[raw-probe] prompt sent, listening for events...\n")

                start = time.monotonic()
                async for sse in source.aiter_sse():
                    if not sse.data:
                        continue
                    try:
                        raw = json.loads(sse.data)
                    except json.JSONDecodeError:
                        print(f"[raw]  NON-JSON: {sse.data[:120]}")
                        continue

                    # Unwrap sync wrapper if present
                    payload = raw.get("payload", raw)
                    event_type = payload.get("type", "?")

                    # Skip unrelated sessions once we know ours
                    props = payload.get("properties") or {}
                    evt_session = props.get("sessionID") or (props.get("info") or {}).get("sessionID")
                    if session_id and evt_session and evt_session != session_id:
                        continue

                    # Capture model info from first assistant message.updated
                    if event_type == "message.updated" and not model_info:
                        info2 = props.get("info") or {}
                        m = info2.get("model") or {}
                        if m.get("modelID"):
                            model_info.append(f"{m.get('providerID','?')}/{m.get('modelID','?')}")

                    # Count for summary — label deltas by the part's actual type
                    if event_type == "message.part.updated":
                        part = props.get("part") or {}
                        ptype = part.get("type", "?")
                        pid = part.get("id", "")
                        if pid:
                            part_types[pid] = ptype
                        has_delta = "delta" in props
                        event_counts[f"part.updated[type={ptype},has_delta={has_delta}]"] += 1
                    elif event_type == "message.part.delta":
                        field = props.get("field", "?")
                        pid = props.get("partID", "")
                        actual_type = part_types.get(pid, "?")
                        event_counts[f"part.delta[field={field},part_type={actual_type}]"] += 1
                    else:
                        event_counts[event_type] += 1

                    # Print raw JSON (compact) + summary
                    compact = json.dumps(raw, separators=(",", ":"))
                    summary = _summarize(payload)
                    elapsed = time.monotonic() - start
                    print(f"[+{elapsed:5.2f}s] {summary}")
                    if len(compact) < 400:
                        print(f"          RAW: {compact}")
                    else:
                        print(f"          RAW: {compact[:400]}…")
                    print()

                    # Detect completion: message.updated with time_end for assistant
                    if event_type == "message.updated":
                        info = props.get("info") or {}
                        t = info.get("time") or {}
                        if info.get("role") == "assistant" and (t.get("completed") or t.get("end")):
                            done.set()
                            return

                    # Also stop on session.idle
                    if event_type == "session.idle":
                        done.set()
                        return

        consume_task = asyncio.create_task(_consume())

        try:
            await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)
        except TimeoutError:
            print(f"[raw-probe] TIMEOUT after {TIMEOUT_S}s")
            consume_task.cancel()
            return 1

        consume_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await consume_task

    print("\n" + "=" * 60)
    if model_info:
        print(f"MODEL: {model_info[0]}")
    print("EVENT BREAKDOWN:")
    for key, count in sorted(event_counts.items()):
        print(f"  {count:4d}x  {key}")

    # Count text deltas that are actually reasoning vs real assistant text
    reasoning_delta = sum(v for k, v in event_counts.items() if "part_type=reasoning" in k)
    real_text_delta = sum(v for k, v in event_counts.items() if "part.delta[field=text" in k and "part_type=reasoning" not in k)
    print()
    print(f"real text deltas (go to TTS):            {real_text_delta}")
    print(f"reasoning deltas masquerading as text:   {reasoning_delta}")
    if reasoning_delta:
        print("  ⚠  thinking content IS leaking to TTS via field='text' on reasoning parts!")
        print("     Fix: filter by part_id in _reasoning_parts set (already in opencode_session.py)")
    else:
        print("  ✓  no reasoning content detected (model may not have thinking enabled)")
    return 0


import contextlib

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
