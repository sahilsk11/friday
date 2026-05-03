"""Tests for the narration policy (filter rules + streaming filter).

Pure-function tests for ``should_speak`` / ``filter_for_speaking`` plus a
streaming suite that drives ``StreamingFilter`` with the same kinds of
delta sequences opencode produces.
"""

from __future__ import annotations

import pytest

from friday.core.narration_policy import (
    StreamingFilter,
    checkpoint_for_tool,
    filter_for_speaking,
    should_speak,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Looking at auth.py", True),
        ("", False),
        ("   ", False),
        ("[tool: read auth.py]", False),
        ("[error: missing key]", False),
        ("[INFO: starting]", False),
        ("$ ls -la", False),
        ("# pip install foo", False),
        ("Hello # not a shell line", True),
        ("Just normal prose", True),
    ],
)
def test_should_speak(text: str, expected: bool) -> None:
    assert should_speak(text) is expected


def test_filter_for_speaking_strips_fenced_blocks() -> None:
    text = "Here is the fix:\n```python\nx = 1\n```\nThat should work."
    assert filter_for_speaking(text) == "Here is the fix:\nThat should work."


def test_filter_for_speaking_strips_log_prefix_lines() -> None:
    text = "Starting work\n[tool: read foo]\n[debug: something]\nDone"
    assert filter_for_speaking(text) == "Starting work\nDone"


def test_filter_for_speaking_handles_tilde_fences() -> None:
    text = "Try this:\n~~~\nfoo\n~~~\nMakes sense?"
    assert filter_for_speaking(text) == "Try this:\nMakes sense?"


def test_filter_for_speaking_keeps_unclosed_fence_open() -> None:
    """If a fence is open and never closes, everything after it is dropped."""
    text = "Here it is:\n```\nfoo bar\nstill code"
    assert filter_for_speaking(text) == "Here it is:"


# ── StreamingFilter ─────────────────────────────────────────────────────────


def test_streaming_filter_passthrough_for_plain_text() -> None:
    f = StreamingFilter()
    assert f.feed("Hello ") == "Hello "
    assert f.feed("world.") == "world."
    assert not f.inside_fence


def test_streaming_filter_strips_fence_in_single_delta() -> None:
    f = StreamingFilter()
    out = f.feed("see this: ```py\nx=1\n``` done")
    assert out == "see this:  done"
    assert not f.inside_fence


def test_streaming_filter_handles_split_open_delimiter() -> None:
    """Deltas that split the opening ``` across two arrivals."""
    f = StreamingFilter()
    # Delta 1 ends with two backticks — must be held, not emitted.
    assert f.feed("look here ``") == "look here "
    # Delta 2 completes the fence and adds in-fence content + closer.
    assert f.feed("`py\nx=1\n``` done") == " done"
    assert not f.inside_fence


def test_streaming_filter_short_runs_pass_through_when_outside_fence() -> None:
    """Inline ``code`` (single/double backticks) shouldn't be eaten."""
    f = StreamingFilter()
    out = f.feed("call `foo()` then bar.")
    assert out == "call `foo()` then bar."


def test_streaming_filter_reset_clears_fence_state() -> None:
    f = StreamingFilter()
    f.feed("opening ```")  # inside fence after this
    assert f.inside_fence
    f.reset()
    assert not f.inside_fence
    assert f.feed("fresh start") == "fresh start"


def test_streaming_filter_emits_nothing_inside_fence() -> None:
    f = StreamingFilter()
    f.feed("```\n")
    assert f.feed("def hello():") == ""
    assert f.feed("    pass\n") == ""
    assert f.feed("```\nback to prose") == "\nback to prose"


def test_streaming_filter_flushes_held_chars_when_no_more_room() -> None:
    """Two backticks at the start of one delta, third never arrives."""
    f = StreamingFilter()
    f.feed("``")  # held
    # Subsequent non-backtick chars should release the held content
    assert f.feed("a") == "``a"


# ── tool checkpoints ────────────────────────────────────────────────────────


def test_checkpoint_for_known_tools() -> None:
    assert checkpoint_for_tool("read") == "looking at a file"
    assert checkpoint_for_tool("Bash") == "running a command"  # case-insensitive


def test_checkpoint_for_unknown_tool_is_none() -> None:
    assert checkpoint_for_tool("madeup_tool") is None
