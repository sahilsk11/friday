# Transport & Data Flow

How friday's voice room talks to the browser, how the agent talks to opencode,
and how state moves between them. Read this before touching anything that
crosses the wire.

---

## The protocols, in one paragraph each

**HTTP** is one-shot: client asks, server replies, done. Fine for "list my
sessions", terrible for live audio.

**WebSocket** is HTTP's persistent cousin. The browser does a one-time
handshake with the server and then both sides can send messages back and
forth over a single TCP connection until someone closes it. Reliable,
ordered, simple. Connect time: milliseconds. We use it for live voice.

**Server-Sent Events (SSE)** is one-way streaming over plain HTTP — server
pushes, client listens. Built-in browser API (`EventSource`), built-in
auto-reconnect. We use it for the standalone transcript page, which doesn't
need a return channel from the browser.

**WebRTC** is peer-to-peer audio/video designed for Zoom-style calls between
two phones on different networks. Uses UDP, does NAT traversal (the "ICE
candidates" thing), negotiates encryption. The handshake takes 8–15 seconds
in our setup because it tries every possible network path. We **don't use
it.** Browser and server share a machine in dev and an origin behind Caddy
in prod — WebRTC's complexity buys nothing. We tried it, ate the latency,
and switched.

---

## Pipecat in one paragraph

Pipecat is a Python framework for assembling audio pipelines as a list of
`FrameProcessor`s. Frames flow downstream through the list. Each processor
reads frames it cares about, ignores the rest, optionally produces new
frames. Conceptually a linear pipeline (pipecat *can* branch via
`ParallelPipeline`, but ours doesn't). Each processor runs in its own
asyncio task, so they execute concurrently — "linear" describes data flow,
not threading.

Our pipeline, per voice connection:

```
transport.input → STT → OpencodeProcessor → TTS → transport.output → RTVIProcessor
```

- `transport.input` — receives audio frames from the browser WebSocket
- `STT` — ElevenLabs realtime, emits transcription frames
- `OpencodeProcessor` — our custom piece; consumes finalized transcripts,
  drives opencode, emits assistant text frames
- `TTS` — ElevenLabs, turns text frames into audio frames
- `transport.output` — ships audio frames back to the browser
- `RTVIProcessor` — collects control messages from upstream and ships them
  to the browser on the same WebSocket

---

## RTVI

RTVI is pipecat's control-plane protocol. Audio frames are *one* kind of
thing flowing through the pipeline. RTVI messages are *another* — control
events, multiplexed onto the same WebSocket as audio. Examples:

- `bot-started-speaking` / `bot-stopped-speaking`
- `user-transcription` (interim and final)
- `user-started-speaking` / `user-stopped-speaking`
- custom server messages (we use these for opencode events)

You don't author the protocol. `RTVIProcessor` is already in the pipeline.
The browser-side pipecat client demultiplexes RTVI from audio and exposes
events via React hooks. `voice-ui-kit` is built on those hooks.

---

## voice-ui-kit

A React component library — visualizers, transcript overlays, control bars
— that consumes RTVI events from a `<PipecatClientProvider>`.

**One catch.** The kit ships an app shell, `<PipecatAppBase>`, that hardcodes
`transportType: 'smallwebrtc' | 'daily'`. That shell is WebRTC-only and we
can't use it. The *components* inside it are not transport-locked — they
read RTVI, which doesn't care how the WebSocket got there. So we use the
components, skip the shell. A handful of components query camera APIs
unconditionally (`selectedCam`, `isSharingScreen`); those don't work with
WebSocketTransport and we replace them as needed. The visualizer,
connect/mute buttons, and transcript display work fine.

---

## The full picture

### Voice Room (`/s/:id`)

One WebSocket from browser to friday. Audio + RTVI multiplexed.

```
                          Voice Room (/s/:id)
  ┌─────────────────────────────────────────────────────────────┐
  │ Browser  ←──── ONE WebSocket ────→  friday                  │
  │                                                             │
  │ uplink:    audio frames (16 kHz mono PCM, ~20 ms chunks)    │
  │ downlink:  audio frames (24 kHz TTS chunks)                 │
  │            RTVI: interim transcripts                        │
  │            RTVI: final transcripts                          │
  │            RTVI: bot-speaking start/stop                    │
  │            RTVI: user-speaking start/stop                   │
  │            RTVI: tool-started / tool-finished               │
  │            RTVI: assistant-text-delta                       │
  └─────────────────────────────────────────────────────────────┘
```

### Transcript Page (`/s/:id/transcript`)

No voice. Just reads the persisted conversation.

```
                       Transcript Page (/s/:id/transcript)
  ┌─────────────────────────────────────────────────────────────┐
  │ Browser  ←──── REST + SSE ────→  friday                     │
  │                                                             │
  │ REST  GET /api/sessions/:id            (load history)       │
  │ SSE   GET /api/sessions/:id/stream     (live updates)       │
  └─────────────────────────────────────────────────────────────┘
```

### Sessions List (`/`)

Plain REST.

```
  Browser  ──── REST ────→  friday
    GET  /api/sessions
    POST /api/sessions
```

---

## What's actually on the WebSocket

### Audio: a minute of you talking, in numbers

Mic capture: 16 kHz, mono, 16-bit PCM. Every ~20 ms the recorder packages
up a chunk of samples (~640 bytes raw audio) into an `InputAudioRawFrame`:

```
InputAudioRawFrame {
  audio: bytes(640),       # the PCM samples
  sample_rate: 16000,
  num_channels: 1
}
```

Protobuf-encoded, sent as one binary WebSocket message. ~50 messages per
second. A minute of talking ≈ 3,000 binary messages totaling ~2 MB. Tiny by
network standards.

The chunks are *not* independent — they're a continuous stream of samples.
The recorder slices small so latency stays low.

### RTVI: JSON text frames

Sent as text WebSocket messages, interleaved with binary audio. Shape
varies by message type. Example committed-transcript message:

```json
{
  "label": "rtvi-ai",
  "type": "user-transcription",
  "data": {
    "text": "what does this code do",
    "user_id": "...",
    "timestamp": "2026-05-03T...",
    "final": true
  }
}
```

### Three WebSockets per voice session, total

The browser only sees one. friday opens two more behind the scenes:

1. Browser ↔ friday  (audio + RTVI)
2. friday ↔ ElevenLabs STT  (audio up, transcript events down)
3. friday ↔ ElevenLabs TTS  (text up, audio down)

(2) and (3) live inside their respective pipecat services.

---

## How ElevenLabs STT actually works

The STT processor opens a WebSocket from friday to ElevenLabs when the
pipeline starts and keeps it open the whole session. As audio chunks
arrive at friday from the browser, the STT processor pulls the PCM bytes
out of each frame and forwards them up the ElevenLabs WebSocket.

ElevenLabs incrementally runs its STT model on the streaming audio and
sends back **two kinds of events**:

- **Interim transcripts**: "what" → "what does" → "what does this" → "what
  does this co—" → "what does this code do". Each interim revises the
  previous guess; pipecat wraps them as `InterimTranscriptionFrame`s.
  These flow downstream and `RTVIProcessor` republishes them as
  `user-transcription` RTVI events with `final: false`.
- **Committed transcripts**: when ElevenLabs' built-in VAD sees 1.5 s of
  silence after speech, it locks in the current best guess and emits a
  final commit event. Pipecat wraps it as a finalized `TranscriptionFrame`
  — *that's* what triggers `OpencodeProcessor` to send the turn to
  opencode.

The 1.5 s silence isn't *required* to do transcription — it's required to
declare "this thought is done, send it to the agent." Without it, every
utterance would just append to one infinite running sentence and the agent
would never get a turn.

We previously had pipecat's Silero VAD upstream of STT, which cut audio
off after 200 ms of silence and starved ElevenLabs of the trailing audio
it needed to commit. We removed Silero; STT does its own VAD now.

---

## How opencode events come back

`OpencodeProcessor` lives mid-pipeline. When it pushes `frame.text` to
opencode, opencode streams events back over its own HTTP+SSE API. Each
event becomes:

- A **TextFrame** flowing downstream toward TTS (so the bot speaks the
  reply).
- A **RTVI server message** pushed sideways to `RTVIProcessor`, which
  ships it to the browser. Examples:
  - `{type: "tool-started", name: "read", args: {...}}`
  - `{type: "tool-finished", name: "read", result_summary: "..."}`
  - `{type: "assistant-text-delta", text: "..."}`

These are not voice-state events — they're app events — but they ride RTVI
because they belong to the live voice session. Putting them on RTVI keeps
the voice room single-channel. The standalone transcript page reads the
persisted equivalent over SSE.

This bends jarvis.md's earlier "RTVI is voice-UI state only" guideline.
The bend is intentional: for the voice room, the agent's tool activity
*is* voice-UI state — it's what tells the user the system is alive while
opencode thinks for 30 seconds. SSE stays for the non-voice transcript
page.

---

## The end-to-end trip a single utterance takes

You speak: "what does this code do?"

1. Mic stream → 50 chunks/second → each becomes an `InputAudioRawFrame`,
   protobuf-encoded → binary WebSocket messages → friday.
2. Pipeline `transport.input` deserializes and forwards frames downstream.
3. STT processor extracts PCM, sends to ElevenLabs WebSocket. ElevenLabs
   streams back interims: `"what"`, `"what does"`, `"what does this"`, …
4. Each interim becomes an `InterimTranscriptionFrame`. `RTVIProcessor`
   republishes as `user-transcription { final: false }`. Browser receives
   them on its WebSocket and updates the transcript display live.
5. You stop talking. After 1.5 s, ElevenLabs commits: `"what does this
   code do"`. Pipecat emits a finalized `TranscriptionFrame`. `RTVIProcessor`
   republishes as `user-transcription { final: true }`.
6. `OpencodeProcessor` consumes the final transcript. Calls
   `session.send_turn("what does this code do")`. Listens on opencode's
   event stream.
7. Opencode says: tool `grep` started → `OpencodeProcessor` emits RTVI
   server message `tool-started`. Browser shows "running grep". Tool
   finishes → `tool-finished`. Repeat for each tool.
8. Opencode streams back assistant text: "this hook subscribes to…".
   `OpencodeProcessor` emits `TextFrame`s downstream → TTS turns text into
   audio frames → `transport.output` ships them back. *Also* emits
   `assistant-text-delta` RTVI messages so the browser can display the
   text as it speaks.
9. Browser plays the audio. Bot-speaking RTVI events drive the visualizer.
10. Conversation persists to friday's session store. Next time you load
    `/s/:id/transcript`, REST returns the history; SSE streams any further
    updates.

---

## Where each piece of UI state comes from

| Surface | Source | Channel |
|---|---|---|
| Mic level meter | local `getUserMedia` + AnalyserNode | client-only |
| User-speaking indicator | RTVI `user-started-speaking` | WS / RTVI |
| Bot-speaking indicator | RTVI `bot-started-speaking` | WS / RTVI |
| Live partial transcript | RTVI `user-transcription { final: false }` | WS / RTVI |
| Final transcript history (voice room) | RTVI `user-transcription { final: true }` + `assistant-text-delta` | WS / RTVI |
| Final transcript history (transcript page) | persisted opencode events | REST + SSE |
| Tool activity | RTVI `tool-started` / `tool-finished` (custom server msg) | WS / RTVI |
| Connect / transport state | `usePipecatClientTransportState()` | client-only (state machine) |

---

## Why these choices and not others

- **WebSocket over WebRTC:** sub-second connect, no NAT traversal needed
  (browser+server share a machine).
- **WebSocket over SSE for voice:** SSE is one-way; voice needs a return
  channel for the mic.
- **SSE over WebSocket for the read-only transcript page:** the page has
  no voice pipeline to spin up. SSE is one connection, one direction,
  built-in reconnect, cheaper than negotiating a WebSocket and the
  pipeline behind it.
- **RTVI for voice-room app events instead of a parallel SSE:** keeps the
  voice room single-channel and ordered relative to audio. Don't bypass
  pipecat to send raw WebSocket messages — you'd race the audio pipeline.
- **ElevenLabs realtime STT instead of batch:** we want interim
  transcripts for the live UI and we want to commit on built-in VAD
  silence rather than waiting for a full upload.
- **No Silero VAD upstream of STT:** the local VAD's `stop_secs=0.2` cut
  off audio before ElevenLabs' `vad_silence_threshold_secs=1.5` saw enough
  trailing silence to commit. Letting STT do its own VAD is the simpler,
  working path.
- **`WavMediaManager` over the default `DailyMediaManager`:** Daily's
  manager pulls in `@daily-co/daily-js` which spins up a WebRTC call
  object purely for mic capture. `WavMediaManager` just calls
  `getUserMedia` and uses the Web Audio API — lighter, works in headless
  Chromium where Daily's video device probing fails.
