"""End-to-end check: SYSTEM_PROMPT_VOICE rides on send_turn and shapes output.

Asks for something that *would* normally produce markdown + a preamble. Reply
should be plain prose with no backticks/bullets/headers and no "got it" /
"here's" / "sure" preamble.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from friday.core.opencode_provider import SYSTEM_PROMPT_VOICE, OpencodeProvider

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
PROMPT = "Walk me through three ways to install Python on macOS."
PREAMBLE_PHRASES = [
    "got it",
    "sure",
    "okay,",
    "ok,",
    "here's",
    "here are",
    "let me",
    "i'll",
    "i can",
    "of course",
    "happy to",
]
MARKDOWN_PATTERNS = [
    (r"```", "code fence"),
    (r"`[^`]+`", "inline backticks"),
    (r"^\s*[-*]\s", "bullet list"),
    (r"^\s*\d+\.\s", "numbered list"),
    (r"^\s*#+\s", "header"),
    (r"\*\*[^*]+\*\*", "bold"),
]


async def main() -> int:
    print(f"[probe] base_url={BASE_URL}")
    final: list[str] = []
    done = asyncio.Event()

    async with OpencodeProvider(BASE_URL) as client:
        session = await client.new_session(title="probe-voice-prompt")

        async def on_final(text: str) -> None:
            final.append(text)
            done.set()

        session.on_text_final(on_final)
        await session.send_turn(PROMPT, system=SYSTEM_PROMPT_VOICE)
        try:
            await asyncio.wait_for(done.wait(), timeout=90.0)
        except TimeoutError:
            print("[probe] TIMEOUT")
            await session.cancel()
            return 1

    reply = final[0]
    print(f"\n[probe] reply ({len(reply)} chars):\n{reply}\n")

    lower_first_line = reply.strip().split("\n")[0].lower()
    preamble_hits = [p for p in PREAMBLE_PHRASES if lower_first_line.startswith(p)]
    md_hits = [
        label for pat, label in MARKDOWN_PATTERNS if re.search(pat, reply, flags=re.MULTILINE)
    ]

    print(f"[probe] first-line preamble hits: {preamble_hits}")
    print(f"[probe] markdown hits:            {md_hits}")

    if not preamble_hits and not md_hits:
        print("[probe] PASS — reply is preamble-free and markdown-free")
        return 0
    print("[probe] FAIL — prompt isn't fully in effect")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
