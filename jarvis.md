# Jarvis — Voice Interface for opencode

A personal voice webpage. Open from anywhere, talk naturally about what you're building, opencode does the work, narrates progress, can be interrupted, sessions persist across disconnects.

---

## Vision

Talk to a webpage like Jarvis. Dictate what you want built. The system acknowledges immediately, kicks off opencode in the background, narrates checkpoints as it works, and lets you interrupt or redirect at any time. Close your phone, come back hours later, ask "where are we at" — the same opencode session is still running and ready to report.

Differs from ChatGPT voice mode: that's text-in/text-out wrapped in audio. This is voice-driven autonomous work where opencode actually executes.

---

## Requirements (from lived 12-hour test of friday v1)

These are real, observed pain points — not speculation.

1. **Barge-in, not turn-stacking.** When you speak, it always has a visible effect: interrupt the bot, replace what's queued, never silently pile up unheard input.
2. **Visible listening state at all times.** Mic level, listening/transcribing/speaking badges. You should never wonder "is it hearing me?"
3. **First-class sessions.** Not "the current conversation" — a list you pick from, resume into, glance at later. Driven by the actual usage pattern: fire prompt, close phone, come back hours later, ask what happened.
4. **Decoupled lifecycles.** opencode keeps running when the voice client disconnects. The voice layer is an ephemeral view onto a persistent agent.
5. **Immediate acknowledgement** sub-second after the user finishes speaking, before opencode loads the codebase.
6. **Checkpoint narration** during long-running agent work — "looking at auth.py", "about to make changes, should I proceed?"
7. **Queued user input** that injects at safe points between tool calls (not silently swallowed).
8. **No TTS overlap.** One speech stream at a time. The friday v1 bug where two TTS responses talked over each other must not return.

---

## Framework decision: pipecat

Considered: pipecat, LiveKit, custom (continue friday v1).

Pipecat and LiveKit are roughly equivalent on capabilities for this use case. Both solve interruption, VAD, TTS overlap, audio streaming, frontend SDKs. The deciding factor was deployment fit with the existing sas Ansible setup:

| | Pipecat | LiveKit |
|-|-|-|
| Ansible roles needed | 1 | 2–3 |
| Processes on box (new) | 1 (FastAPI) | 4 (livekit-server, agent worker, FastAPI, frontend host) |
| Runtimes to install | Python | Go + Python + JS |
| Frontend SDK | voice-ui-kit | LiveKit Components React |
| Self-hosting fit | Drop into sas pattern | Larger ops surface |

LiveKit primitives (`session.say()`, `SpeechHandle`, `AsyncToolset`) are well-shaped — we mirror their API in framework-neutral abstractions so a future swap is mechanical.

Continuing friday v1 was rejected: the bugs (queueing, no interrupt, TTS overlap) are exactly what a real framework solves for free, and the custom transport/audio code is dead weight.

---

## Architecture

### Design discipline

**The voice framework only owns audio in/out and per-session pipeline assembly. Everything else is framework-neutral Python.** If a module imports `pipecat.*`, it's throwaway when we swap. If not, it survives.

### Code layout

```
jarvis/
├── server/
│   ├── jarvis/
│   │   ├── core/                  # zero pipecat imports
│   │   │   ├── opencode_session.py
│   │   │   ├── session_registry.py
│   │   │   ├── narration_policy.py
│   │   │   ├── speech_chunker.py
│   │   │   ├── voice_observer.py  # Protocol mirroring LiveKit's API
│   │   │   ├── events.py          # typed event schema
│   │   │   └── state.py           # AgentState enum
│   │   ├── api/                   # framework-neutral REST
│   │   │   └── sessions.py
│   │   └── voice/                 # ★ throwaway on framework swap
│   │       ├── pipecat_adapter.py
│   │       └── server.py
│   ├── tests/
│   │   └── test_opencode_session.py
│   └── pyproject.toml
│
├── web/                           # Vite + React
│   ├── src/
│   │   ├── pages/
│   │   │   ├── SessionsList.tsx   # framework-neutral
│   │   │   ├── SessionView.tsx    # framework-neutral
│   │   │   └── VoiceRoom.tsx      # ★ imports voice-ui-kit
│   │   ├── hooks/
│   │   │   └── useVoiceState.ts   # ★ wraps voice-ui-kit
│   │   └── api/
│   │       └── client.ts          # REST client
│   └── package.json
│
└── ansible/role/friday/           # contributed to sas repo
    ├── tasks/main.yml
    ├── templates/
    │   ├── friday-server.service.j2
    │   ├── Caddyfile.j2
    │   └── friday.env.j2
    └── handlers/main.yml
```

### Layered responsibilities

| Layer | Responsibility | Framework imports? |
|-|-|-|
| `core/opencode_session` | opencode HTTP + SSE wrapper | No |
| `core/session_registry` | session metadata, sqlite, lifetime | No |
| `core/narration_policy` | event → speech decision (pure) | No |
| `core/speech_chunker` | text deltas → TTS-sized chunks (pure) | No |
| `core/voice_observer` | Protocol implemented by voice layers | No |
| `core/events` | typed event schema | No |
| `api/sessions` | REST CRUD + SSE for live updates | No |
| `voice/pipecat_adapter` | bridges core → pipecat frames | Yes |
| `voice/server` | pipeline assembly + /api/offer | Yes |
| `web/pages/Sessions*` | session management UI | No |
| `web/pages/VoiceRoom` | voice room UI | Yes |
| `web/hooks/useVoiceState` | wraps voice-ui-kit hooks | Yes |

### Key abstraction: VoiceObserver

```python
# core/voice_observer.py — no framework imports
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

Shape mirrors LiveKit's API on purpose. Pipecat adapter implements via `TTSSpeakFrame` + observers. A future LiveKit adapter implements natively. Call sites only import the Protocol.

### Persistent session model

```
opencode (HTTP server, long-lived)
    ↑  long-lived SSE subscription
OpencodeSession (Python object, lives in SessionRegistry)
    ↑  attach/detach by voice clients
Pipecat pipeline (per-connection, ephemeral)
    ↑  WebRTC
Browser (Vite + voice-ui-kit)
```

- Voice client disconnects → pipeline torn down → OpencodeSession unaffected
- Voice client reconnects to `/s/:id` → new pipeline attaches to existing OpencodeSession
- Server restarts → SessionRegistry rehydrates from sqlite, re-subscribes to opencode SSE for any still-running sessions

---

## Deployment

### Constraints from sas/PREVIEWS.md

- Cloudflare Access tunnel does NOT reliably transport browser WebSockets
- Tunnel cannot carry UDP / WebRTC media
- Firewall currently allows only port 22

### Solution: dedicated subdomain bypassing the tunnel

- DNS: `voice.<domain>` A-record → VPS public IP, **not proxied through CF**
- Caddy on box terminates TLS
- Open UDP port range (e.g. 50000–50100) for WebRTC media
- App-level auth: shared token in URL (`?t=<long-random>`), bookmarked. CF Access not used here.

### Ansible role footprint

~100 lines YAML. One new role added to sas playbook.

```
roles/friday/
├── tasks/main.yml
│   ├── ufw allow UDP 50000:50100
│   ├── ufw allow TCP 443
│   ├── install Caddy + Python venv
│   ├── render systemd unit + Caddyfile
│   └── enable + start friday-server.service
├── templates/...
└── handlers/main.yml
```

### Process footprint

- `friday-server.service` (Python FastAPI + pipecat) — new
- `opencode-tmux.service` — already on box, friday hits via localhost HTTP
- `caddy.service` — new (or shared with future services)

---

## Salvage from friday v1

~500 LOC of empirical opencode integration knowledge worth porting. Other ~1500 LOC reinvents what pipecat + voice-ui-kit give for free.

| File | LOC | Action |
|-|-|-|
| `agent/opencodeAdapter.ts` | 342 | Port to `core/opencode_session.py`. Preserve event types, SSE reconnect/watchdog logic, the "fire done on message.updated" insight (without it queued turns get stuck) |
| `agent/types.ts` | 31 | Inform `core/voice_observer.py` shape |
| `pipelines/speakingPolicy.ts` | 44 | Port to `core/narration_policy.py` |
| `pipelines/ttsChunker.ts` | 87 | Port to `core/speech_chunker.py` (verify pipecat's chunking isn't already adequate) |
| `wsServer.ts` | 275 | Discard — replaced by SmallWebRTCTransport |
| `protocol.ts` | 164 | Discard — replaced by RTVI |
| `sessionManager.ts` | 326 | Re-express as ~80 LOC pipecat processor |
| `stt/*`, `tts/*` | 428 | Discard — pipecat ships providers |
| `frontend/*` | ~1900 | Discard — voice-ui-kit replaces |

---

## Build plan

Each step independently testable. Steps 1–3 work without ever touching audio.

### Step 1: OpencodeSession + CLI harness

Prove opencode wrapper works with persistence/attach/detach semantics.

- Port friday v1's `opencodeAdapter.ts` to Python
- Wraps opencode HTTP API + SSE event stream
- `attach(observer)` / `detach(observer)` for ephemeral observers
- CLI: `jarvis attach <id>` opens stdin/stdout view onto a session
- Multiple terminals attach to same session — all see same events
- Close one terminal — session keeps going
- Validates: persistence, fan-out, reconnect

### Step 2: SessionRegistry + sqlite

Sessions survive server restart.

- In-memory `dict[id → OpencodeSession]`
- sqlite tables:
  - `sessions(id, opencode_session_id, title, created_at, last_activity)`
  - `messages(id, session_id, role, text, ts)`
- On startup, rehydrate sessions from sqlite, re-subscribe to opencode SSE for any still-running ones
- CLI: `jarvis list`, `jarvis new <title>`, `jarvis attach <id>`

### Step 3: REST API

HTTP surface for the frontend.

- FastAPI app
- `GET /sessions` — list with metadata
- `POST /sessions` — create new (creates opencode session too)
- `GET /sessions/:id` — metadata + transcript
- `GET /sessions/:id/events` — SSE stream of live updates
- `POST /sessions/:id/turn` — text turn (also used by voice path after STT)
- `POST /sessions/:id/cancel` — interrupt current run

All framework-neutral. No pipecat imports.

### Step 4: Voice plumbing (pipecat)

Talk to opencode through the browser.

- `voice/pipecat_adapter.py`: `OpencodeProcessor(FrameProcessor)` implements VoiceObserver, bridges OpencodeSession events → TTSSpeakFrames
- `voice/server.py`: FastAPI route `/api/offer` for WebRTC signaling, mounts pipecat SmallWebRTCTransport
- Pipeline: `transport.input() → STT → user_aggregator → OpencodeProcessor → tts → transport.output()`
- STT/TTS choice: start with cloud providers (Deepgram + Cartesia) for sub-second latency. Switch to local (Whisper + Kokoro) later if cost matters.

### Step 5: Frontend

Usable webpage.

- Vite + React app (no Next.js — simpler hosting)
- `/` SessionsList — REST-backed, "New" and "Resume" buttons, last-activity timestamps
- `/s/:id` VoiceRoom — voice-ui-kit primitives composed with custom session header showing live state
- `/s/:id/transcript` SessionView — read-only transcript history
- All app state via REST + SSE. RTVI is not used for app data.

### Step 6: Deploy

Live on `voice.<domain>`.

- Add `ansible/roles/friday/` to sas
- `sas deploy`
- Bookmark URL with auth token
- Verify from phone

---

## Future-proofing: if LiveKit becomes preferable

1. Delete `voice/`
2. Write `voice/livekit_adapter.py` implementing same VoiceObserver Protocol (~150 LOC)
3. Write `voice/agent.py` LiveKit agent worker
4. Add `livekit-server` + `livekit-agent` Ansible roles
5. Frontend: replace `VoiceRoom.tsx` + `useVoiceState.ts`. Other pages untouched.

Estimated effort: a weekend. `core/`, `api/`, sessions UI, deployment subdomain all unchanged.

### Anti-patterns that make swap painful — avoid

- Putting session state on a pipecat `FrameProcessor` instance (state lives in OpencodeSession/sqlite)
- Using RTVI custom messages for app data (use REST/SSE on dedicated endpoints)
- Importing `pipecat.frames` outside `voice/`
- Letting `<ConsoleTemplate>` own the whole voice page (compose primitives instead)
- Encoding pipecat-specific event shapes in sqlite (use own typed events)

---

## Open questions to resolve during build

1. **STT/TTS provider:** Deepgram + Cartesia (cloud, ~300ms latency, paid) vs Whisper + Kokoro (local, slower, free). Default cloud for v1; revisit if latency or cost forces it.
2. **Immediate ack:** separate fast LLM (Groq Llama 3.1 8B) generating dynamic acks, or pre-rendered audio clips for canned phrases ("On it"). Clip is faster and cheaper but less responsive to context.
3. **Auth token scheme:** static shared token (simple, fine for personal) vs rotating per-session JWT. Static for v1.
4. **UDP port range:** start 50000–50100, adjust based on observed concurrent call needs (probably 1).
5. **Title generation for sessions:** synchronous on first turn, or async after first agent response? Sync risks slowing the first turn; async means list shows "Untitled" briefly.

---

## Out of scope for v1

- Multi-user
- OAuth / proper auth UI (shared token is fine)
- Multiple simultaneous voice rooms
- Mobile native app (browser is the target)
- Other agent backends (Claude Code, Hermes) — design supports via VoiceObserver but only opencode wired

---

## Reference repos (for design lookup, not forks)

- `~/Projects/pipecat` — framework source, key reads: `examples/transports/transports-small-webrtc.py`, `src/pipecat/services/llm_service.py`, `src/pipecat/transports/smallwebrtc/`
- `~/Projects/voice-ui-kit` — frontend components, key read: `examples/04-vite/`
- `~/Projects/friday` — v1 attempt, salvage source for opencode integration patterns
- `~/Projects/sas` — Ansible setup, deployment target
- `https://deepwiki.com/pipecat-ai/pipecat` — high-level architecture docs

LiveKit reference (in case of future swap):
- `livekit/agents` — Python agent framework
- `livekit/livekit` — Go SFU server
- `livekit/components-react` — frontend components
