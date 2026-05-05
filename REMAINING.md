# REMAINING.md — context to carry forward

This branch (`voice/provider-abstraction-wiring`) makes the Provider abstraction real: one file per backend, application code only types against `Provider` / `ProviderSession`, persistence (list/get sessions, transcripts, models) is on the Protocol. ClaudeCode is wired end-to-end against the real Agent SDK.

What's not done, what's known to be wrong, and what would surprise the next person to pick this up.

---

## Known bugs

### Voice WebSocket creates sessions without a directory
[voice/server.py:124](server/friday/voice/server.py:124) — `await provider.create_session()` is called with no `directory`. After the fix in this PR, `POST /sessions` requires it, but the WS path bypasses that validation. Two cleanups are needed together:

1. The WS route should be **attach-only**: reject (close 1003) when `?session_id=` is missing, force callers to `POST /sessions` first.
2. Drop the create-on-WS branch entirely.

The frontend currently has both paths wired (it might POST first, or it might rely on the WS auto-create). Audit `web/src/` before changing the server contract.

### Voice path can mid-turn-reconnect into a fresh ClaudeCode wrapper if cache eviction happens
Not currently triggered (cache is never evicted), but if/when LRU eviction is added (#leak below), an in-flight `query()` task could be orphaned: events fan into a wrapper whose observers were torn down by the previous WS disconnect, and the new wrapper has no clue. Mitigation: cache eviction should never evict a session whose `_query_task` is running.

---

## Known leaks

### Provider session caches grow unbounded
Both `OpencodeProvider._sessions` and `ClaudeCodeProvider._sessions` are append-only `dict[str, Session]`. Sessions never get evicted. For a long-running server this is a memory leak proportional to total sessions ever created. Not blocking — friday is one user per machine — but worth bounding.

For OpencodeProvider the dict is **load-bearing**: it's the SSE dispatch table, not just a cache. Routing depends on `self._sessions.get(session_id)`. So eviction needs to be coordinated with "no observers attached + no in-flight turn" — ref-counted by attach()/detach().

For ClaudeCodeProvider the dict is **defensive symmetry** — it gives session-identity (same id → same wrapper) so future multi-observer scenarios work, but no current code relies on it. Could remove and have `attach()` always return a fresh wrapper, at the cost of losing in-memory `current_state` continuity across reconnects.

### Frontend wire shape (`ModelRef` in `api/sessions.py`) inherits opencode camelCase
`providerID` / `modelID` field names in [api/sessions.py:42](server/friday/api/sessions.py:42) mirrored opencode's wire originally. They're now friday's API shape but still broadcast the heritage. Renaming would break the frontend; not worth doing on its own.

---

## Architectural questions still open

### `current_state` is a true mirror of harness state
The application caches the latest `AgentState` (THINKING/IDLE/etc.) on each session because:

- **Opencode** doesn't expose a `GET /session/{id}/state` endpoint. State is only learnable by subscribing to SSE. We mirror it because the alternative (poll an SSE topic for state) is worse than caching the last value seen.
- **ClaudeCode** state is *defined* by what our process does — there's no remote state to query.

Adding a state endpoint to opencode upstream would let us drop the mirror. Out of scope for friday today.

### "Use harness as source of truth" — where it applies and where it doesn't
We discussed this on the branch; summary:

- **Truly local** (cannot be sourced anywhere else): observer callbacks, the SSE dispatch table, dedup sets (`_completed`, `_announced_tools`).
- **Synthesis from streaming events** (derived, not duplicated): `_accumulated[message_id]` composes delta streams into full text for `on_text_final`.
- **Mirror** (could be eliminated, with trade-offs): `current_state`. That's the only one.

Not really a leak — most of what we hold is unavoidable. But "we hoard data the harness has" was a fair instinct to interrogate.

---

## Hacks worth knowing about

### `directory` is required on every claude-code `send_turn`, not just at creation
[claude_code_provider.py:113](server/friday/core/claude_code_provider.py:113) — counterintuitive: we pass `opts.cwd = self.directory` on **every** turn, not just the first. Reason: Claude Code's session store is on disk at `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. The CLI uses cwd as a **lookup key** to find the session file, even when resuming via `opts.resume = session_id`. Drop the cwd and `resume` fails with "No conversation found." So the cwd is a path component, not redundant state.

### Claude-code multi-turn requires `opts.resume`
[claude_code_provider.py:117](server/friday/core/claude_code_provider.py:117) — every turn after the first must pass `opts.resume = self.id`, otherwise the SDK creates a *new* session each turn and conversation history is lost. Earlier the code shipped without this and the probe didn't catch it because it only sent one turn. If you change the send_turn body, **add a multi-turn integration test** so this doesn't regress silently.

### Voice WebSocket has no `_resolve_provider` guard for graceful 503
[voice/server.py:96](server/friday/voice/server.py:96) — if the provider isn't ready when a WS connects, we `raise RuntimeError` instead of closing the socket cleanly. The HTTP path returns 503 via FastAPI's HTTPException; the WS path doesn't have an equivalent and the runtime error bubbles out of FastAPI ungracefully. Should `await websocket.close(code=1011)` instead.

### `next_turn_model` and `narrate_tools` live on the pipecat processor, not the session
[pipecat_adapter.py:85](server/friday/voice/pipecat_adapter.py:85) — these toggles ride along on RTVI client messages and are stamped on the `ProviderSessionProcessor` instance. They're not part of the `ProviderSession` Protocol. Fine for now (they're voice-UI concerns), but if you ever want to drive a session from the HTTP API with the same toggles, they need to move onto the session.

---

## Things to do before this branch fully replaces the old stack

1. **Frontend audit + WS attach-only.** See "Voice WebSocket creates sessions without a directory" above. Frontend probably has a code path that opens WS without `?session_id=` and relies on the server creating one. That has to stop, then the server bypass can be removed.

2. **Multi-turn integration test for claude-code.** The probe is a single-turn smoke test. Add a test that sends two turns and asserts the second turn has memory of the first (e.g., "remember 42" → "what number?" → expect `42` in result).

3. **Provider conformance test for the persistence methods.** [test_provider_conformance.py](server/tests/test_provider_conformance.py) only checks `isinstance` against `Provider` / `ProviderSession`. Doesn't exercise `list_sessions`, `get_transcript`, etc. against real backends. Add at least one round-trip per method per provider.

4. **Decide what `list_models()` returns for claude-code.** Currently a hardcoded static list of three claude models. If Anthropic ships a new model, this constant has to be bumped manually. Acceptable today; not great long-term.

5. **`get_session()` for a non-existent claude-code session raises `LookupError`.** [claude_code_provider.py:265](server/friday/core/claude_code_provider.py:265). The HTTP layer translates `Exception` → 500. Should be a 404 — wrap in HTTPException at the API boundary, or define a `SessionNotFound` exception in `provider.py` that the API layer catches.

6. **`SessionInfo.title` on claude-code uses summary fallback.** [claude_code_provider.py:296](server/friday/core/claude_code_provider.py:296) — `custom_title or summary or ""`. The SDK's `summary` is auto-generated from the first user message and is often very long. The transcripts list shows truncated titles by necessity. Consider truncating in the provider rather than letting every UI decide.

---

## Frontend changes that will be needed

When you switch the frontend to the new BE contract:

- `POST /sessions` now **requires** `{directory, title?}`. Sending no directory returns 422.
- `GET /sessions/{id}` response shape is unchanged.
- `POST /sessions/{id}/turn` is unchanged.
- `GET /models` is unchanged at the wire level (still `{providerID, modelID, providerName, modelName}` per row, optional default).
- `WS /api/voice` — once attach-only is enforced, opening without `?session_id=` will close immediately.

---

## Branch state

- Branch: `voice/provider-abstraction-wiring`
- 6 commits ahead of master
- All 91 tests pass; pyright clean on `friday/` and `tests/`; ClaudeCode probe works end-to-end (single turn) and multi-turn resume verified by hand.
- One unmerged dependency: `claude-agent-sdk>=0.1.73` added to pyproject in commit 97fd8b1.

The PR is structurally complete from the BE side. The remaining work is the frontend cutover (forces directory in the create form, drops the WS-creates-session fallback) and the test/UX polish above.
