# Backend Integration

How friday's backend is shaped today, and the contract a frontend (voice-ui-kit or otherwise) needs to integrate against.

> **Status note.** Everything below describes the code on disk and is verified by tests. The offer endpoint's signaling round-trip is exercised by [`tests/test_voice_offer.py`](server/tests/test_voice_offer.py) using a real `aiortc` peer. The voice path's narration/tool/ack behavior is exercised by [`scripts/probe_narration.py`](server/scripts/probe_narration.py) against live opencode + ElevenLabs. The unverified piece is the browser↔server WebRTC handshake itself — fine in theory (voice-ui-kit uses the same `@pipecat-ai/small-webrtc-transport` we tested against) but only proven when a real call connects.

---

## What friday is

A bridge between a browser voice client (mic + speaker over WebRTC) and `opencode`, the coding agent. Opencode owns sessions, transcripts, and tool execution. Friday owns audio I/O, narration filtering, and the per-call pipeline. The browser owns the UI.

```
Browser  ⇄  friday/server (FastAPI + pipecat)  ⇄  opencode HTTP+SSE
   ▲           ▲                                       ▲
   │           │                                       │
   └─ WebRTC ──┘                                       │
       audio                                  long-lived process,
                                              session storage at
                                              ~/.local/share/opencode/
```

The repo lives at `friday-v2/`. The server is in [server/](server/); there is no frontend yet.

---

## Process model

Three things have to be running for end-to-end voice to work:

| Process | Default URL | Owns |
|---|---|---|
| `opencode serve` | `http://127.0.0.1:4096` | sessions, transcripts, tool execution |
| `friday` (uvicorn) | `http://127.0.0.1:8765` | REST/SSE, WebRTC signaling, voice pipeline |
| Browser (voice-ui-kit, eventually) | — | mic, speaker, UI |

`friday` connects to opencode at startup using `OPENCODE_BASE_URL` (default above) and holds one HTTP client + one SSE subscription for the lifetime of the process. Each WebRTC connection spawns a per-call pipecat pipeline; the opencode HTTP/SSE side is shared.

---

## REST surface

Lives in [server/friday/api/sessions.py](server/friday/api/sessions.py). All routes are framework-neutral — no pipecat imports. The voice path uses these too.

### `GET /sessions`

List sessions opencode knows about. Optional `?directory=...` filter.

```json
[
  {
    "id": "ses_21317b...",
    "title": "alpha",
    "directory": "/Users/sahil.kapur/Projects/friday-v2",
    "created_at": "2026-05-03T07:55:11+00:00",
    "updated_at": "2026-05-03T07:55:14+00:00"
  }
]
```

### `POST /sessions`

Create a new opencode session. Body `{"title"?: string}` → returns one `SessionRow` like the rows above. Status `201`.

### `GET /sessions/{id}`

Metadata + full transcript:

```json
{
  "session": { …same shape as a row above… },
  "transcript": [
    { "role": "user",      "text": "list files",   "completed_at": null },
    { "role": "assistant", "text": "Here they…",   "completed_at": "2026-05-03T07:55:14+00:00" }
  ]
}
```

### `POST /sessions/{id}/turn`

Send a text turn. Body `{"text": "..."}` → `202 Accepted` with `{"session_id": "ses_…"}`. Forwarded to opencode immediately; opencode queues if a turn is in flight. The voice path POSTs here after STT — text-only clients can use the same endpoint.

### `GET /sessions/{id}/events` (SSE)

Server-Sent Events stream of live deltas + state for one session. Frame types:

```
data: {"type": "state", "state": "thinking"}
data: {"type": "text.delta", "text": "Hello"}
data: {"type": "text.delta", "text": " world"}
data: {"type": "text.final", "text": "Hello world"}
data: {"type": "state", "state": "idle"}
```

Plus a `: keep-alive\n\n` comment every 15 seconds when idle so browsers/proxies don't drop the stream. Disconnect with `EventSource.close()`; the server cleans up.

State values: `idle | listening | thinking | speaking` (from [core/state.py](server/friday/core/state.py)).

A frontend that wants to render the transcript live (without doing voice) only needs `GET /sessions/{id}` + `GET /sessions/{id}/events` + `POST /sessions/{id}/turn`. RTVI is **not** used for app data — keep it that way; it's only there for voice-UI state surfacing.

---

## Voice surface (WebRTC)

Lives in [server/friday/voice/server.py](server/friday/voice/server.py). The frontend exchanges SDP with us; we run a per-call pipecat pipeline:

```
transport.input()
    → VADProcessor (Silero)
    → STT (ElevenLabs realtime, falls back to Deepgram)
    → OpencodeProcessor          # the bridge — replaces the LLM slot
    → TTS (ElevenLabs, falls back to Cartesia)
    → transport.output()
    → RTVIProcessor              # surfaces voice-UI state to voice-ui-kit
```

Provider selection is env-driven, see [server/friday/voice/server.py:_select_stt](server/friday/voice/server.py) and `_select_tts`. ElevenLabs wins when its key is present; force with `FRIDAY_STT_PROVIDER` / `FRIDAY_TTS_PROVIDER`. `FRIDAY_TTS_VOICE_ID` overrides the default voice.

### `POST /api/offer`

WebRTC SDP exchange. The pipecat JS client (`@pipecat-ai/small-webrtc-transport`, which voice-ui-kit uses internally) sends:

```json
{
  "sdp": "v=0\r\n…",
  "type": "offer",
  "pc_id": null,                    // string when reconnecting / renegotiating
  "restart_pc": false,
  "request_data": { "session_id": "ses_…"  /* optional: attach to existing */ }
}
```

Returns:

```json
{
  "sdp": "v=0\r\n…",
  "type": "answer",
  "pc_id": "<server-assigned id>"
}
```

When `request_data.session_id` is omitted or null, friday creates a new opencode session for this call. When provided, friday attaches to the existing one — the user picks up where they left off.

### `PATCH /api/offer`

Trickled ICE candidates after the initial answer:

```json
{
  "pc_id": "<from-the-answer>",
  "candidates": [{ "candidate": "candidate:…", "sdpMid": "0", "sdpMLineIndex": 0 }]
}
```

### What the voice path actually does

1. `OpencodeProcessor.process_frame(TranscriptionFrame(finalized=True))` → `POST /sessions/{id}/turn`. Forwarded immediately even if a turn is in flight; opencode queues. Friday never calls `/abort`.
2. While opencode is `busy` and no assistant text has streamed yet, friday pushes a `TTSSpeakFrame("on it")` — the **immediate ack**. Suppressed on duplicate `busy` events and on early text deltas.
3. As opencode emits `message.part.delta` events, every delta runs through [`StreamingFilter`](server/friday/core/narration_policy.py) (strips ``` ... ``` fences across deltas) before becoming `LLMTextFrame`s. Pipecat's TTS service does NLTK sentence aggregation downstream.
4. On `message.part.updated` for a tool, friday pushes `TTSSpeakFrame("looking at a file")` etc. — **checkpoint narration**. Phrasing in [core/narration_policy.py:_TOOL_VERBS](server/friday/core/narration_policy.py).
5. On `message.updated` with `time.completed`, friday emits `LLMFullResponseEndFrame`, which flushes the TTS aggregator.

---

## Known limits

### CORS

`friday/main.py` ships with `CORSMiddleware` allowing `http://localhost:5173`, `http://localhost:3000`, and the `127.0.0.1` equivalents — covers Vite and Next.js dev servers out of the box. Override with `FRIDAY_CORS_ORIGINS` (comma-separated, or `*` for any) if your dev origin is different. In production, frontend + backend share an origin behind Caddy and CORS is moot.

### Auth (Step 6 in [PLAN.md](PLAN.md))

No auth today. The `/api/offer` endpoint is open. Step 6 will add bearer-token auth on REST routes and `?t=...` query auth on the offer endpoint (browsers can't add `Authorization` headers to WebRTC offers). **Until that lands, only run friday on localhost.**

### Browser↔server WebRTC handshake

Verified in test up to "the offer applies cleanly to a real `aiortc` peer." Verified in production by Pipecat's published demos using the same `@pipecat-ai/small-webrtc-transport` voice-ui-kit pulls in. Not verified yet by an actual `friday` ↔ browser session — that's the first thing to confirm when the frontend lands.

---

## Will voice-ui-kit "just work"?

**Yes, as far as the backend can prove.** The offer endpoint is at `/api/offer` (matches voice-ui-kit's default `connectParams.webrtcUrl`), accepts the body shape `@pipecat-ai/small-webrtc-transport` produces, and is verified by a test that mints a real SDP offer with `aiortc`, posts it, and applies the answer.

A fresh frontend session needs to:

1. Use `<PipecatAppBase connectParams={{ webrtcUrl: "/api/offer" }} transportType="smallwebrtc">` (or point `webrtcUrl` at the full server URL during dev — both work).
2. Pass `request_data: { session_id }` when attaching to an existing opencode session; omit it for "new session." The backend creates an opencode session lazily when no id is supplied.
3. Build the session list and transcript pages as plain React + REST/SSE — voice-ui-kit isn't involved there, and shouldn't be (per the [Jarvis](jarvis.md) separation rules: only the voice-room page imports voice-ui-kit).

The reference example to follow is [voice-ui-kit/examples/04-vite/src/main.tsx](../voice-ui-kit/examples/04-vite/src/main.tsx) — `<VoiceVisualizer>`, `<UserAudioControl>`, `<ConnectButton>` composed against the resulting client.

### Suggested prompt for a fresh frontend session

> friday's backend is at `friday-v2/server/`. Read [BackendIntegration.md](friday-v2/BackendIntegration.md) and [jarvis.md](friday-v2/jarvis.md) to understand the contract and FE/BE separation rules.
>
> Build a Vite + React frontend in `friday-v2/web/`. Three pages, per `jarvis.md`:
> - `/` — SessionsList. Plain React + REST against `GET /sessions`. No voice-ui-kit imports.
> - `/s/:id` — VoiceRoom. **The only page that imports voice-ui-kit.** Use `<PipecatAppBase transportType="smallwebrtc" connectParams={{ webrtcUrl: "/api/offer" }}>` and pass `request_data: { session_id }` so it attaches to the existing opencode session.
> - `/s/:id/transcript` — SessionView. Plain React + `GET /sessions/:id` and SSE on `GET /sessions/:id/events`. No voice-ui-kit imports.
>
> Hard rules: no RTVI custom messages for app data — REST/SSE only. No `pipecat.frames` or voice-ui-kit imports outside the voice room page and its dedicated hook.
>
> First thing to verify: open the voice room, watch ICE state in the network tab, confirm the offer/answer round-trips and you can hear an `"on it"` ack within ~500ms of speaking. If anything 4xx's, check `FRIDAY_CORS_ORIGINS` and the dev server origin.

That session can run in parallel with backend Step 6 (auth).

---

## What you can verify today, without a frontend

End-to-end backend behavior is provable in three places:

- **Unit + integration tests** — `cd server && make check` runs 63 tests including narration filter, fence-stripping, tool checkpoints, REST routes, SSE frame shape, and the WebRTC offer round-trip.
- **WebRTC offer round-trip test** — [`tests/test_voice_offer.py`](server/tests/test_voice_offer.py) mints a real SDP offer with `aiortc`, posts it to `/api/offer`, and applies the returned answer to the local peer. If the answer SDP is malformed or the route's body parsing breaks, this fails.
- **Live behavior probe** — `cd server && uv run python scripts/probe_narration.py` drives a real opencode session through a prompt that produces mixed prose + a code fence + a `read` tool call. It captures the exact frames `OpencodeProcessor` would push, asserts no code leaks into spoken text, and synthesizes the result via ElevenLabs to `/tmp/friday_narration_probe.mp3`.

The voice path is verified through the SDP exchange and through TTS audio bytes. The only piece that needs an actual browser to confirm is the ICE/media flow under real network conditions.
