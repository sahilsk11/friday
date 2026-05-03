# Friday v2 — Build Plan

A voice interface for `opencode`. Talk to a webpage. Opencode does the work, narrates progress, sessions survive across disconnects.

---

## Principles

- **Pipecat owns audio. Opencode owns the agent. Friday is the bridge.**
- `core/` and `api/` never import `pipecat.*`. Pipecat lives only in `voice/` so it's swappable.
- Test against real services. No mocked opencode, no mocked WebRTC for end-to-end checks.
- Strict types, dead-code lint, ≤700 line files (`make check`).
- Backend before voice. Voice before frontend. Deploy last.

---

## Required behaviors

The bar for "this works." Each derives from a real friday v1 pain point.

1. **Immediate ack.** When the user stops speaking, friday speaks back inside ~500ms (e.g. "on it") *while* opencode is still loading the codebase. Achieved by listening to opencode's SSE — when we see `session.status: busy` for the user's turn but no assistant text deltas yet, push a canned `TTSSpeakFrame` outside the normal LLM path.

2. **Sequential turn queueing — handled by opencode.** When the user speaks mid-turn, friday forwards the new turn directly via `/prompt_async`. Opencode queues it and processes it after the current turn finishes. Friday never calls `/abort` and never queues at our layer. Verified empirically: back-to-back `/prompt_async` calls drain in order, both turns complete cleanly. Cost: "steering" is delayed until the current turn ends. v2 can add an explicit cancel gesture if that latency hurts.

3. **No TTS overlap.** One speech stream at a time. Pipecat handles this for free via its single TTS frame queue. We must not push our own out-of-band TTS while regular text is streaming — verify in tests.

4. **Visible listening state.** RTVI observer surfaces user-speaking / bot-speaking / transcribing to voice-ui-kit so the UI can show mic levels and badges. The user never wonders "is it hearing me?"

5. **Persistent sessions.** Opencode owns session storage (`~/.local/share/opencode/`). Friday is stateless across restarts — list and read sessions via opencode's HTTP API. Phone disconnects → opencode keeps running. Reconnect → re-attach to the same session, see what happened.

6. **Checkpoint narration.** When opencode runs tools (`message.part.updated` with `type: tool`), friday narrates short summaries ("looking at auth.py", "running the tests"). Same TTS path as regular text, gated by a narration policy that skips code blocks and log noise.

---

## Architecture

```
opencode (external HTTP+SSE — owns session + transcript storage)
        ↑
   OpencodeClient  ──  one global SSE subscription, fan-out by session_id
        ↑
   OpencodeSession (per opencode session, in-memory only)
        ↑ event observers
   ┌────┴─────────────────────────┐
   │                              │
 REST/SSE              voice pipeline (pipecat)
 (api/sessions.py)            ↑
                          SmallWebRTC ← browser
```

App data flows through REST/SSE. RTVI is used **only** to surface voice-UI state (mic levels, listening badges) — not for sessions, transcripts, or events. That separation is what makes the voice layer swappable.

### Opencode endpoints we hit

Opencode owns persistence; friday is a thin façade. The full set of endpoints we use:

| Endpoint | Method | Purpose | Verified |
|---|---|---|---|
| `/session` | `POST` | Create a new session. Body `{"title"?: str}` → returns `{id, slug, ...}`. | ✅ |
| `/session` | `GET` | List all sessions across all directories. | ✅ |
| `/session/:id` | `GET` | Session metadata (title, directory, created, updated). | ✅ |
| `/session/:id/message` | `GET` | Full transcript: `[{info: {role, time: {created, completed?}}, parts: [...]}]`. | ✅ |
| `/session/:id/prompt_async` | `POST` | Send a turn. Body `{"parts":[{"type":"text","text":"..."}]}` → 204. Queued by opencode if a turn is in-flight. | ✅ |
| `/session/:id/abort` | `POST` | Cancel in-flight turn. Returns `true`. **Not used in v1** — kept available for an explicit "stop" gesture later. | ✅ |
| `/global/event` | `GET` (SSE) | Stream of all events for all sessions. Wrapped `{directory, project, payload: {type, properties}}`. | ✅ |

Two endpoints we deliberately don't use:

- `POST /session/:id/message` — synchronous prompt that blocks until completion. We use `prompt_async` + SSE instead so we can stream tokens to TTS as they arrive.
- `DELETE /session/:id` — session deletion. Not exposed in friday v1; can add when there's a UI need.

Auth: opencode 1.14 has no per-request auth on these endpoints. Friday-server itself is auth-protected (Step 7); opencode lives behind it on localhost.

---

## Step 1 — OpencodeSession + event parser  ✅ done

Built and tested live against opencode 1.14:

- [`core/events.py`](server/friday/core/events.py) — typed parser for the SSE wire format. Discards `sync` wrappers; surfaces `MessagePartDelta`, `MessageUpdated`, `SessionStatus`, `SessionIdle`, `MessagePartUpdated`, `ServerConnected`.
- [`core/opencode_session.py`](server/friday/core/opencode_session.py) — `OpencodeClient` (owns httpx + the single SSE loop with reconnect/generation counter) and `OpencodeSession` (per-session: `attach`, `send_turn`, `cancel`).
- [`core/state.py`](server/friday/core/state.py) — `AgentState` enum.
- [`scripts/probe_opencode.py`](server/scripts/probe_opencode.py) — live integration probe; verified streaming text deltas, `time.completed` completion detection (opencode 1.14 changed this from `time.end`), state transitions, clean shutdown.

Quirk captured in [opencode_session.py:_fan_out_state](server/friday/core/opencode_session.py): opencode emits the same terminal state multiple times — consumers must be idempotent.

---

## Step 2 — SessionManager (opencode is the SoT)  ✅ done

No sqlite, no friday-side persistence. Opencode already stores every session and transcript at `~/.local/share/opencode/`. Friday-server holds an in-memory cache of *live* `OpencodeSession` objects (so observers can attach) and delegates everything else to opencode's HTTP API.

**Typed wrappers** (in `friday/core/session_manager.py` or co-located in `events.py`):

- `SessionInfo(id, title, directory, created_at, updated_at)` — flattened from `GET /session/:id`.
- `Message(role, text, completed_at, parts)` — flattened from opencode's `{info, parts}` shape returned by `GET /session/:id/message`.

**`SessionManager`**:

- Owns `dict[session_id → OpencodeSession]` — purely runtime cache, lazily populated.
- `await manager.list_sessions() -> list[SessionInfo]` — wraps `GET /session`. Optionally filters to a working directory.
- `await manager.get(session_id: str) -> SessionInfo` — wraps `GET /session/:id`.
- `await manager.get_transcript(session_id: str) -> list[Message]` — wraps `GET /session/:id/message`.
- `await manager.create(title: str | None = None) -> OpencodeSession` — `POST /session`, register in cache, return live wrapper.
- `manager.attach(session_id: str) -> OpencodeSession` — return cached wrapper or create one (so observers can subscribe).

**Tests** (`tests/test_session_manager.py`):

- Unit tests with `pytest-httpx`: canned responses for each endpoint; assert manager surfaces typed data correctly.
- One live integration test asserting `list` and `get_transcript` work against real opencode.

**Deliverable** — `scripts/probe_session_manager.py` against live opencode prints:

```
[probe] listed N existing sessions
[probe] created session ses_abc...
[probe] sent turn: "say hi"
[probe] transcript after completion:
  [user]      say hi
  [assistant] HI
[probe] PASS
```

Files: `friday/core/session_manager.py`, `tests/test_session_manager.py`, `scripts/probe_session_manager.py`.

---

## Step 3 — REST + SSE API  ✅ done

The HTTP surface that both the CLI and the voice layer use.

- FastAPI app composed in [`friday/main.py`](server/friday/main.py). Lifespan owns the `OpencodeClient` and exposes a `SessionManager` via `app.state`. Mounts:
  - `GET    /sessions[?directory=…]` — list (optional directory filter)
  - `POST   /sessions` — create (returns full `SessionRow` after a follow-up `GET`)
  - `GET    /sessions/:id` — metadata + transcript
  - `GET    /sessions/:id/events` — SSE stream of `text.delta` / `text.final` / `state` frames, with 15s `: keep-alive` ping
  - `POST   /sessions/:id/turn` — text turn (voice layer POSTs here after STT)
- Framework-neutral: no pipecat imports.
- Tests: 7 cases in [`tests/test_api_sessions.py`](server/tests/test_api_sessions.py) — pytest-httpx canned responses for the opencode side, ASGITransport for HTTP routes, direct route invocation + `body_iterator` for the SSE path (ASGITransport buffers streaming responses, which deadlocks an in-process SSE consumer; the live verification covers the wire side).
- Verified live against opencode 1.14: `uvicorn friday.main:app` → create session → `POST /turn` → SSE stream emitted `state:thinking → text.delta:"DEL" → text.delta:"TA" → text.final:"DELTA" → state:idle`.

Files: [`friday/api/sessions.py`](server/friday/api/sessions.py), [`friday/main.py`](server/friday/main.py), [`tests/test_api_sessions.py`](server/tests/test_api_sessions.py).

---

## Step 4 — CLI harness  ⏭ skipped

Skipped: a long-lived `click` CLI is dead weight when ad-hoc Python scripts against `/sessions/...` already exercise the same surface and get thrown away after use. If we ever need an interactive testbed before voice ships, write a one-off script — don't grow a CLI.

---

## Step 4 — Voice pipeline (pipecat)  ✅ done

- [`voice/pipecat_adapter.py`](server/friday/voice/pipecat_adapter.py) — `OpencodeProcessor(FrameProcessor)`:
  - Consumes `TranscriptionFrame(finalized=True)` → POST `/sessions/:id/turn`.
  - Subscribes to the bound `OpencodeSession`; emits `LLMFullResponseStartFrame → LLMTextFrame* → LLMFullResponseEndFrame` for each assistant response.
  - Immediate ack: pushes `TTSSpeakFrame("on it")` once when `session.status:busy` arrives before any text deltas. Suppressed if real text starts first or on duplicate busy events.
- [`voice/server.py`](server/friday/voice/server.py) — mounts `POST/PATCH /voice/api/offer` on the same FastAPI app. Each new connection runs a per-call pipeline: `transport.input() → VADProcessor(Silero) → DeepgramSTT → OpencodeProcessor → CartesiaTTS → transport.output() → RTVIProcessor` with `RTVIObserver`. `request_data.session_id` selects which opencode session to attach to (creates one if absent).
- 8 unit tests in [`tests/test_pipecat_adapter.py`](server/tests/test_pipecat_adapter.py): `process_frame` + observer-callback paths exercised via `OpencodeSession.dispatch` with `pytest-httpx` for the wire side. `push_frame` is replaced with a capture list so we don't need a full pipecat lifecycle.

Files: [`friday/voice/pipecat_adapter.py`](server/friday/voice/pipecat_adapter.py), [`friday/voice/server.py`](server/friday/voice/server.py), [`tests/test_pipecat_adapter.py`](server/tests/test_pipecat_adapter.py).

---

## Step 5 — Narration policy + chunking

Make the voice say the right things at the right granularity.

- `core/narration_policy.py` — pure functions: `should_speak(text)`, `filter_for_speaking(text)`. Rules: skip empty / code fences / shell prompts / `[tool:`-style log prefixes. Port from friday v1's `pipelines/speakingPolicy.ts`.
- `core/speech_chunker.py` — buffer + flush at sentence boundary / max chars / max delay. **Only port if pipecat's `sentence` aggregator is insufficient.** Verify first.
- **Checkpoint narration** — when `MessagePartUpdated` with `part_type == "tool"` arrives, generate a short TTS summary ("looking at auth.py", "running the tests"). Same TTS path as regular text.

Files: `friday/core/narration_policy.py`, `friday/core/speech_chunker.py` (maybe), `tests/test_narration_policy.py`.

---

## Step 6 — Configuration + auth

Ready to deploy.

- `friday/config.py` — `pydantic-settings` reading `FRIDAY_*` env vars (`OPENCODE_BASE_URL`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, `FRIDAY_AUTH_TOKEN`, `FRIDAY_DB_PATH`).
- Bearer-token auth middleware on the REST API. Single shared token via `FRIDAY_AUTH_TOKEN` for v1.
- `/api/offer` accepts the token via `?t=...` query param (browsers can't add Authorization headers to WebRTC offers).

Files: `friday/config.py`, `friday/main.py` (auth wiring), `tests/test_config.py`.

---

## Out of scope (for v1)

- Frontend (separate plan once the backend is solid)
- Multiple simultaneous voice rooms
- OAuth / multi-user
- Mobile native app (browser is the target)
- Other agent backends (Claude Code, Hermes) — design supports them via the same `OpencodeSession`-shaped interface, but only opencode is wired

---

## Open questions to resolve during build

- **STT/TTS provider.** Default Deepgram + Cartesia (cloud, ~300ms latency). Revisit if cost or latency forces local (Whisper + Kokoro).
- **Title generation.** Sync on first turn (slow first reply) vs async after first response (UI shows "Untitled" briefly). Lean async.
- **Mid-turn cancel UX.** v1 ships without one (rely on opencode queueing). Revisit if "wait for current turn to finish" feels too slow in practice — likely add a "stop" wake-word or button rather than auto-aborting on every barge-in.
- **Session filtering.** Opencode's `GET /session` returns sessions for *all* directories on this machine. v1 may show all of them; later we may filter to a friday working directory.
- **UDP port range.** Start 50000–50100 for WebRTC media; adjust based on observed concurrent calls.
