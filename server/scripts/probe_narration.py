"""Live behavior probe: real opencode + real narration filter + real ElevenLabs.

What this verifies (beyond unit tests):

1. The narration filter sees real opencode delta sequences and produces
   speakable text without code blocks.
2. Tool checkpoints fire on real opencode tool invocations (this run prompts
   the model to read a file, which triggers a ``read`` tool part).
3. The filtered text is synthesizable end-to-end by ElevenLabs — the script
   writes the resulting audio to disk so a human can listen back and
   confirm nothing weird leaked through.

Run::

    cd server
    uv run python scripts/probe_narration.py

Requires: opencode running on ``OPENCODE_BASE_URL`` (default
``http://127.0.0.1:4096``) and ``ELEVENLABS_API_KEY`` in the environment
(loaded from ``../.env`` if present).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from friday.core.opencode_session import OpencodeClient
from friday.voice.pipecat_adapter import OpencodeProcessor

PROMPT = (
    "Briefly read the file 'PLAN.md' at the repo root and tell me in two short "
    "sentences what step 4 covers. Then give me a tiny example python snippet "
    "(in a fenced code block) of how to call requests.get. Keep your reply under 80 words."
)
OUT_PATH = Path("/tmp/friday_narration_probe.mp3")
ELEVEN_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel — no voices_read needed
ELEVEN_TTS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    base_url = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    eleven_key = os.environ.get("ELEVENLABS_API_KEY")
    if not eleven_key:
        print("[probe] ELEVENLABS_API_KEY not set", file=sys.stderr)
        return 2

    print(f"[probe] opencode: {base_url}")
    client = OpencodeClient(base_url)
    await client.start()
    try:
        session = await client.new_session(title="friday narration probe")
        print(f"[probe] created session: {session.id}")

        captured: list[Frame] = []
        finished = asyncio.Event()
        proc = OpencodeProcessor(session)

        async def capture(
            frame: Frame, _direction: FrameDirection = FrameDirection.DOWNSTREAM
        ) -> None:
            # Intentionally don't forward to the real ``push_frame`` — there's
            # no downstream processor here and the base class would log
            # "StartFrame not received yet" warnings.
            captured.append(frame)
            if isinstance(frame, LLMFullResponseEndFrame):
                finished.set()

        proc.push_frame = capture  # type: ignore[method-assign]

        print(f"[probe] sending turn: {PROMPT!r}")
        await proc.process_frame(_fake_transcription(PROMPT), FrameDirection.DOWNSTREAM)

        try:
            await asyncio.wait_for(finished.wait(), timeout=120)
        except TimeoutError:
            print("[probe] timed out waiting for response", file=sys.stderr)
            return 3

        # Pull the raw transcript from opencode for side-by-side comparison.
        raw = await _fetch_assistant_text(client.http, session.id)
        spoken_text, ack_text, tool_phrases = _bucket_frames(captured)

        print()
        print("=" * 72)
        print("RAW assistant message (from opencode transcript):")
        print("-" * 72)
        print(raw)
        print()
        print("SPOKEN text (LLMTextFrames concatenated, fences stripped):")
        print("-" * 72)
        print(spoken_text or "<nothing>")
        print()
        print(f"IMMEDIATE ACK frames: {ack_text or '<none>'}")
        print(f"TOOL CHECKPOINT frames: {tool_phrases or '<none>'}")
        print("=" * 72)
        print()

        ok = _check_invariants(raw=raw, spoken=spoken_text, tools=tool_phrases)
        if not ok:
            return 4

        # Real synth of the assembled spoken stream.
        full_voice_script = _assemble_voice_script(ack_text, tool_phrases, spoken_text)
        if not full_voice_script.strip():
            print("[probe] nothing to synth — bailing", file=sys.stderr)
            return 5
        print(f"[probe] synth via ElevenLabs ({len(full_voice_script)} chars)…")
        audio = await _synth(eleven_key, full_voice_script)
        OUT_PATH.write_bytes(audio)
        print(f"[probe] wrote {len(audio)} bytes -> {OUT_PATH}")
        print(f"[probe] play it: afplay {OUT_PATH}")
        print("[probe] PASS")
        return 0
    finally:
        await client.aclose()


def _fake_transcription(text: str):  # type: ignore[no-untyped-def]
    from pipecat.frames.frames import TranscriptionFrame

    return TranscriptionFrame(
        text=text, user_id="probe", timestamp="2026-01-01T00:00:00Z", finalized=True
    )


async def _fetch_assistant_text(http: httpx.AsyncClient, session_id: str) -> str:
    resp = await http.get(f"/session/{session_id}/message")
    resp.raise_for_status()
    rows = resp.json()
    parts: list[str] = []
    for row in rows:
        info = row.get("info") or {}
        if info.get("role") != "assistant":
            continue
        for p in row.get("parts") or []:
            if p.get("type") == "text" and p.get("text"):
                parts.append(p["text"])
    return "".join(parts)


def _bucket_frames(frames: list[Frame]) -> tuple[str, str, list[str]]:
    spoken_chunks: list[str] = []
    speak_chunks: list[str] = []
    for f in frames:
        if isinstance(f, LLMTextFrame):
            spoken_chunks.append(f.text)
        elif isinstance(f, TTSSpeakFrame):
            speak_chunks.append(f.text)
    spoken = "".join(spoken_chunks)
    ack = next((s for s in speak_chunks if s == "on it"), "")
    tools = [s for s in speak_chunks if s != "on it"]
    return spoken, ack, tools


def _check_invariants(*, raw: str, spoken: str, tools: list[str]) -> bool:
    """Hard-coded behavior checks — fail loudly if a regression slips."""
    ok = True
    if "```" in spoken:
        print("[probe] FAIL: code-fence delimiter leaked into spoken text")
        ok = False
    # Anything strictly inside a fenced block in the raw should NOT appear
    # verbatim in spoken. We check a couple of canonical Python tokens.
    for marker in ("requests.get", "import requests", "def "):
        if marker in raw and marker in spoken:
            # The model may also use the term in prose; only fail if it sat
            # purely inside a fence in raw.
            if _is_only_inside_fence(raw, marker):
                print(f"[probe] FAIL: {marker!r} from a code block leaked into spoken text")
                ok = False
    if "PLAN.md" in raw and "looking at a file" not in tools:
        # Heuristic: prompt asked to read a file, so a read tool should fire.
        # If opencode chose a different tool we miss this — print but don't fail.
        print("[probe] WARN: prompt referenced a file but no 'looking at a file' checkpoint fired")
    return ok


def _is_only_inside_fence(text: str, marker: str) -> bool:
    """True if every occurrence of ``marker`` in ``text`` is inside ``` ... ``` ."""
    in_fence = False
    last = 0
    inside_hits = 0
    outside_hits = 0
    parts: list[tuple[int, int, bool]] = []
    i = 0
    while i < len(text):
        if text[i : i + 3] == "```":
            parts.append((last, i, in_fence))
            in_fence = not in_fence
            last = i + 3
            i += 3
            continue
        i += 1
    parts.append((last, len(text), in_fence))
    for start, end, inside in parts:
        n = text.count(marker, start, end)
        if inside:
            inside_hits += n
        else:
            outside_hits += n
    return inside_hits > 0 and outside_hits == 0


def _assemble_voice_script(ack: str, tools: list[str], spoken: str) -> str:
    """Stitch what the user would hear into one synth-ready string."""
    pieces: list[str] = []
    if ack:
        pieces.append(ack + ".")
    pieces.extend(t + "." for t in tools)
    if spoken.strip():
        pieces.append(spoken.strip())
    return " ".join(pieces)


async def _synth(api_key: str, text: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.post(
            ELEVEN_TTS_URL,
            headers={"xi-api-key": api_key, "accept": "audio/mpeg"},
            json={"text": text, "model_id": "eleven_turbo_v2_5"},
        )
        resp.raise_for_status()
        return resp.content


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
