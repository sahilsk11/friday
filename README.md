# AGENTS.md — Friday

Voice interface for opencode. Talk to a webpage, opencode does the work, sessions persist across disconnects.

## Repository layout

```
friday/
├── server/
│   ├── friday/
│   │   ├── core/          # zero pipecat imports — survives any framework swap
│   │   ├── api/           # framework-neutral FastAPI REST + SSE
│   │   └── voice/         # pipecat-specific: throwaway on framework swap
│   ├── tests/
│   └── pyproject.toml
└── web/                   # Vite + React
    └── src/
        ├── pages/
        ├── hooks/
        └── api/
```

## The one rule that governs all structure decisions

**`core/` and `api/` must never import `pipecat.*`.** Everything that could survive swapping pipecat for LiveKit lives there. The only pipecat imports allowed are in `voice/`. Violating this makes a future framework swap a rewrite instead of a weekend.

If you are about to import a pipecat symbol outside `voice/`, stop and ask whether the concept belongs in a framework-neutral abstraction instead.

---

## Module responsibilities

### `core/opencode_session.py`

Wraps the opencode HTTP API and SSE event stream. One `OpencodeSession` object per opencode session — lives in the registry, not on a pipeline.

Key behaviors ported from `~/Projects/friday/backend/src/agent/opencodeAdapter.ts`:
- Single global SSE subscription that fans out to all attached observers
- Watchdog timer (90s idle → force reconnect): the stream can go half-closed silently
- Generation counter to bail stale reconnect loops
- `message.updated` fires `onTextFinal` — without this, queued turns get stuck because `message.part.delta` alone never signals completion
- Exponential backoff on reconnect, capped at 30s

Attach/detach API:
```python
session.attach(observer: VoiceObserver) -> None
session.detach(observer: VoiceObserver) -> None
```
Multiple observers can be attached simultaneously (CLI harness + active voice pipeline).

### `core/session_registry.py`

In-memory `dict[id → OpencodeSession]` backed by sqlite.

Tables:
```sql
sessions(id, opencode_session_id, title, created_at, last_activity)
messages(id, session_id, role, text, ts)
```

On server startup: rehydrate from sqlite, re-subscribe to opencode SSE for any session that was running.

### `core/voice_observer.py`

Protocol that every voice adapter implements. No framework imports.

```python
class VoiceObserver(Protocol):
    async def say(self, text: str) -> SpeechHandle: ...
    async def interrupt_current_speech(self) -> None: ...
    async def on_user_transcript(self, text: str, final: bool) -> None: ...
    async def on_state_change(self, state: AgentState) -> None: ...

class SpeechHandle(Protocol):
    @property
    def interrupted(self) -> bool: ...
    async def wait_for_done(self) -> None: ...
```

Shape mirrors LiveKit's `session.say()` / `SpeechHandle` API intentionally. The pipecat adapter implements via `TTSSpeakFrame` + frame observers.

### `core/narration_policy.py`

Pure function. Decides whether an opencode text delta should be forwarded to TTS.

Port from `~/Projects/friday/backend/src/pipelines/speakingPolicy.ts`. Rules:
- Empty after trim → skip
- Opens a code fence (` ``` ` or `~~~`) → skip
- Shell prompt line (`$ ` or `# `) → skip
- Tool/log prefix (`[tool:`, `[system:`, `[error:`) → skip
- Also strip fenced code blocks from full text before speaking

### `core/speech_chunker.py`

Buffers incoming text deltas, emits TTS-sized chunks.

Port from `~/Projects/friday/backend/src/pipelines/ttsChunker.ts`. Flush triggers:
1. Buffer exceeds `max_chars` (default 200)
2. Buffer ends at sentence boundary (`.!?\n`) when `sentence_boundary=True`
3. `max_delay_ms` timer fires (default 250ms)

Check first whether pipecat's own text aggregation already handles this adequately before porting. Only port if pipecat's chunking is coarser than needed.

### `core/events.py`

Typed event schema. All events flowing through the system are defined here.

### `core/state.py`

`AgentState` enum: `IDLE | LISTENING | THINKING | SPEAKING`.

### `api/sessions.py`

Framework-neutral FastAPI routes. No pipecat imports.

```
GET  /sessions              list with metadata
POST /sessions              create new (also creates opencode session)
GET  /sessions/:id          metadata + transcript
GET  /sessions/:id/events   SSE stream of live updates
POST /sessions/:id/turn     text turn (voice path calls this after STT)
POST /sessions/:id/cancel   interrupt current run
```

### `voice/pipecat_adapter.py`

`OpencodeProcessor(FrameProcessor)` — implements `VoiceObserver`, bridges `OpencodeSession` events → `TTSSpeakFrame`s. The only place that translates between opencode's event model and pipecat's frame model.

### `voice/server.py`

FastAPI route `/api/offer` for WebRTC signaling. Assembles the pipecat pipeline per connection:

```
transport.input() → STT → user_aggregator → OpencodeProcessor → tts → transport.output()
```

Uses `SmallWebRTCTransport`. See `~/Projects/pipecat/examples/transports/transports-small-webrtc.py` for the connection lifecycle (offer/renegotiate, `pcs_map`, background task pattern).

---

## Session persistence model

```
opencode HTTP server  (long-lived, external process)
    ↑  long-lived SSE
OpencodeSession       (lives in SessionRegistry, survives voice disconnects)
    ↑  attach/detach
Pipecat pipeline      (ephemeral, one per WebRTC connection)
    ↑  WebRTC
Browser
```

Voice client disconnects → pipeline torn down → `OpencodeSession.detach()` → session unaffected.
Voice client reconnects → new pipeline → `OpencodeSession.attach()` → picks up where it left off.
Server restarts → `SessionRegistry` rehydrates from sqlite.

---

## Frontend (web/)

Vite + React, TypeScript strict. No Next.js.

Pages:
- `/` — `SessionsList.tsx`: REST-backed list. No voice-ui-kit imports.
- `/s/:id` — `VoiceRoom.tsx`: composes `voice-ui-kit` primitives with session header. Only page that imports voice-ui-kit.
- `/s/:id/transcript` — `SessionView.tsx`: read-only transcript. No voice-ui-kit imports.

`hooks/useVoiceState.ts` wraps voice-ui-kit hooks. All voice-ui-kit coupling is confined to `VoiceRoom.tsx` and this hook.

App state flows through REST + SSE (`/sessions/:id/events`). RTVI is not used for app data.

Reference: `~/Projects/voice-ui-kit/examples/04-vite/` for how to wire voice-ui-kit in a Vite app.

---

## Testing approach

**Every change must be tested before reporting done.** This means:

- `core/` modules: run `pytest` against the specific module. Spin a subagent, call real functions, assert real behavior.
- `api/` routes: start FastAPI with `uvicorn`, hit endpoints with `httpx`, verify JSON shape and SSE stream events.
- `voice/` integration: this requires audio — test the pipeline connection lifecycle (connect, send a text frame, verify TTS output frame, disconnect) without needing a real microphone.
- Frontend: start dev server (`pnpm dev`), open in browser, exercise the golden path.

Do not report a feature working based on reading the code. Run it.

---

## Linting and type rules

- Python: `ruff` for linting, `mypy` with `strict=true`. No `# type: ignore` without a comment explaining why.
- TypeScript: `strict: true` in tsconfig. No `any` without justification.
- Max function length: 40 lines. If a function is longer, split it.
- Max file length: 300 lines. If a file is longer, extract a module.
- These limits are enforced by CI. Don't leave violations expecting a separate cleanup pass.

---

## Anti-patterns — avoid these

- Session state on a `FrameProcessor` instance — state lives in `OpencodeSession` and sqlite, not on the pipeline
- RTVI custom messages for app data — use REST/SSE on dedicated endpoints
- `pipecat.frames` imports outside `voice/`
- Letting one page component own both session logic and voice UI — compose primitives
- Pipecat-specific event shapes in sqlite — use the typed events from `core/events.py`

---

## Reference repos

| Repo | What to read |
|------|--------------|
| `~/Projects/pipecat` | `examples/transports/transports-small-webrtc.py`, `src/pipecat/transports/smallwebrtc/` |
| `~/Projects/voice-ui-kit` | `examples/04-vite/` |
| `~/Projects/friday/backend/src` | `agent/opencodeAdapter.ts` (port to core/opencode_session.py), `pipelines/speakingPolicy.ts`, `pipelines/ttsChunker.ts` |

The friday `wsServer.ts`, `protocol.ts`, `stt/`, `tts/`, and all frontend code are **discarded** — replaced by pipecat + voice-ui-kit.

---

## Build steps (in order)

Each step is independently testable without touching audio.

1. **`core/opencode_session.py`** — port the opencode SSE adapter, CLI harness (`friday attach <id>`)
2. **`core/session_registry.py` + sqlite** — persistence, `friday list` / `friday new`
3. **`api/sessions.py`** — REST + SSE surface
4. **`voice/`** — pipecat pipeline, WebRTC signaling
5. **`web/`** — Vite frontend

Do not start step N until step N-1 passes its tests.
