"""What we narrate vs. swallow.

The voice path runs every assistant text delta through this module before it
reaches TTS. Two concerns:

1. **Streaming fence filter** — strip ``` ... ``` and ``~~~ ... ~~~`` blocks
   so the synth never speaks code. Fences can span multiple deltas, so we
   keep state between calls (``StreamingFilter``).

2. **Line policy** — drop empty / whitespace-only chunks and lines that look
   like agent log prefixes (``[tool: ...]``, ``[system: ...]``, etc.). These
   are pure functions over a single chunk; useful for both delta filtering
   and one-shot final-message cleaning.

3. **Checkpoint phrasing** — ``checkpoint_for_tool(name)`` returns a short
   spoken summary for tool starts (``"looking at the file"``, ``"running a
   command"``). Stays here so the full speaking surface is in one place.

Ported from friday v1 ``backend/src/pipelines/speakingPolicy.ts`` and
expanded for streaming + tools.
"""

from __future__ import annotations

import re

# Triple-or-more backticks/tildes anywhere in the text. v1 only matched at
# line start, but opencode often emits ```python on the same line as prose,
# and treating any 3+ run as a fence delimiter is a safe heuristic — the
# only false positive is the literal phrase "three backticks" in spoken
# prose, which is vanishingly rare.
_FENCE_RE = re.compile(r"`{3,}|~{3,}")

_LOG_PREFIX_RE = re.compile(r"^\[(?:tool|system|error|warn|debug|info):", re.IGNORECASE)
_SHELL_LINE_RE = re.compile(r"^[\$#]\s")

# Friendly phrasing for opencode tool kinds. Kept short on purpose — these
# play during real silence between user turn and assistant response, so
# brevity beats accuracy.
_TOOL_VERBS: dict[str, str] = {
    "read": "looking at a file",
    "edit": "editing the file",
    "write": "writing a new file",
    "bash": "running a command",
    "glob": "searching for files",
    "grep": "searching the codebase",
    "list": "listing files",
    "task": "kicking off a subtask",
    "webfetch": "fetching a page",
    "websearch": "searching the web",
    "todowrite": "updating the todo list",
}


def should_speak(text: str) -> bool:
    """Per-line policy: returns False for empty, log-prefix, or shell lines."""
    trimmed = text.strip()
    if not trimmed:
        return False
    if _LOG_PREFIX_RE.match(trimmed):
        return False
    return not _SHELL_LINE_RE.match(trimmed)


def filter_for_speaking(text: str) -> str:
    """One-shot clean of an entire message.

    Strips fenced code blocks and any lines that fail :func:`should_speak`,
    then collapses runs of blank lines. Intended for use on accumulated
    final text — for streaming, use :class:`StreamingFilter`.
    """
    without_fences = _strip_complete_fences(text)
    kept = [line for line in without_fences.splitlines() if should_speak(line)]
    return "\n".join(kept).strip()


def _strip_complete_fences(text: str) -> str:
    out: list[str] = []
    inside = False
    last = 0
    for match in _FENCE_RE.finditer(text):
        if not inside:
            out.append(text[last : match.start()])
        inside = not inside
        last = match.end()
    if not inside:
        out.append(text[last:])
    return "".join(out)


def checkpoint_for_tool(tool_name: str) -> str | None:
    """Return a spoken summary for a tool start, or None to stay silent.

    Returns ``None`` for unknown tools rather than guessing — better silent
    than a wrong description. Wire new tools into ``_TOOL_VERBS`` as they
    show up in real opencode output.
    """
    return _TOOL_VERBS.get(tool_name.lower())


class StreamingFilter:
    """Stateful filter for narrating an assistant message as it streams.

    Holds two pieces of state across deltas:

    - whether we're currently inside a code fence (so a fence opened in one
      delta can close in a later one);
    - a short tail of recently-seen backtick/tilde chars, so a fence
      delimiter that arrives split across deltas (``"``"`` then ``"`text"``)
      is still recognized.

    Call :meth:`reset` between assistant messages — fence state should not
    leak across turns.
    """

    # We may need to defer up to two backticks of output because they could
    # be the start of a 3+ run that completes in the next delta. Three is
    # always enough for ``` and ~~~ delimiters.
    _MAX_HOLD = 2

    def __init__(self) -> None:
        self._inside = False
        # Held chars that *might* be the start of a fence delimiter and
        # haven't been emitted yet.
        self._held = ""

    def reset(self) -> None:
        self._inside = False
        self._held = ""

    @property
    def inside_fence(self) -> bool:
        return self._inside

    def feed(self, delta: str) -> str:
        """Return the speakable substring of one streaming delta."""
        text = self._held + delta
        self._held = ""
        out: list[str] = []
        i = 0
        while i < len(text):
            char = text[i]
            if char in "`~":
                run = self._fence_run_length(text, i, char)
                if run >= 3:
                    self._inside = not self._inside
                    i += run
                    continue
                # Could be the start of a 3+ run that lands in the next
                # delta — defer if there's no room left to be sure.
                if i + run == len(text) and run <= self._MAX_HOLD:
                    self._held = text[i:]
                    return "".join(out)
                if not self._inside:
                    out.append(text[i : i + run])
                i += run
                continue
            if not self._inside:
                out.append(char)
            i += 1
        return "".join(out)

    @staticmethod
    def _fence_run_length(text: str, start: int, char: str) -> int:
        run = 0
        while start + run < len(text) and text[start + run] == char:
            run += 1
        return run
