# Plan: bring the voice room UX up to spec

Read `TRANSPORT.md` first. This file is the diff between current state and
the model described there.

---

## Current state

- WebSocket voice path works end-to-end (1 s connect, audio in/out, opencode
  turns, TTS reply). Verified with the markdown-table run on 2026-05-03.
- `web/src/pages/VoiceRoom.tsx` is a hand-rolled hook UI with three
  controls: connect button, mute button, transport state pill. No
  visualizer, no transcript display, no tool-activity feed.
- ElevenLabs realtime STT *does* push interim transcripts; we throw them
  away. `RTVIProcessor` already republishes them as RTVI events — nothing
  on the browser side renders them.
- `OpencodeProcessor` consumes finalized transcripts and emits assistant
  text frames toward TTS, but pushes nothing on the RTVI side. Tool
  activity is invisible to the voice room.
- Standalone transcript page (`/s/:id/transcript`) reads REST + SSE and
  works fine. Untouched by this plan.

---

## End state

`/s/:id` voice room shows:

1. Mic level visualizer — visceral "we hear you" feedback.
2. Live partial transcript — your words appear as you speak.
3. Conversation history — your finalized turns and the agent's replies,
   scrollback.
4. Tool-activity feed — "running grep", "reading foo.py".
5. Bot-speaking indicator — visualizer responds to TTS playback.
6. Connect/disconnect + mute, with proper state-machine labels.

All of (2)–(5) come over the existing WebSocket via RTVI. (1) and (6) are
client-only state. No new transport.

UI built from `voice-ui-kit` components composed inside our existing
`<PipecatClientProvider>`. No `<PipecatAppBase>` (WebRTC-locked). No
custom-built visualizer or transcript display unless a kit component
genuinely doesn't work with `WebSocketTransport`.

---

## Backend changes (Python, `server/friday/voice/`)

### 1. Emit RTVI server messages from `OpencodeProcessor`

File: `friday/voice/pipecat_adapter.py`.

When opencode events arrive via `session.events()`, push pipecat
`RTVIServerMessageFrame`s (or equivalent — confirm exact name in pipecat
1.1.0) downstream. Each frame becomes a JSON RTVI event on the browser's
WebSocket.

Mapping from opencode event → RTVI message:

| opencode event | RTVI message `type` | payload |
|---|---|---|
| `tool.start` | `tool-started` | `{ name, args_summary }` |
| `tool.finish` | `tool-finished` | `{ name, result_summary }` |
| assistant message delta | `assistant-text-delta` | `{ text }` |
| assistant message complete | `assistant-text-final` | `{ text }` |
| turn complete | `turn-finished` | `{}` |

Keep payloads compact. The voice room renders these as a feed; full text
is also persisted via the existing transcript stream for the other page.

Tests: extend `tests/test_pipecat_adapter.py` to assert the right RTVI
frames are emitted for a fake opencode event stream.

### 2. (Optional, do later) Strip markdown before TTS

Out of scope for this plan. Track separately. The TTS engine reads pipes
and asterisks aloud; we'll prompt opencode to output plain text for
spoken responses, and/or strip markdown in `OpencodeProcessor` before the
text reaches `TTSService`. Not a blocker for the UX work below.

---

## Frontend changes (`web/src/`)

### 3. Restore `voice-ui-kit` components in `VoiceRoom.tsx`

Keep:
- `useEffect` that constructs `PipecatClient` with `WebSocketTransport` +
  `WavMediaManager` (already correct).
- `pcClient.initDevices()` call (required for mic to start streaming).
- `<PipecatClientProvider>` wrapper.

Replace the hand-rolled buttons + pill with kit components:

- `<VoiceVisualizer>` (mic + bot waveform) — confirm it works against
  `WebSocketTransport`. If it queries camera APIs, swap for a 30-line
  custom analyser on the local mic stream.
- `<ConnectButton>` (or kit's named equivalent) — handles connecting /
  connected / error states.
- `<MicMuteButton>` (or equivalent) — wraps `usePipecatClientMicControl`.
- `<UserTranscriptOverlay>` / `<TranscriptDisplay>` — renders
  `user-transcription` RTVI events (interim + final). Confirm exact
  component name when wiring.

Build a small `<ActivityFeed>` component (ours, not the kit's) that uses
`useRTVIClientEvent` to subscribe to `tool-started` / `tool-finished` /
`assistant-text-delta` / `assistant-text-final` and renders them as a
chronological list. ~80 lines.

### 4. Layout

Two-column flex inside the existing shell:

```
┌─ header (← sessions / transcript link) ────────────────────────┐
│                                                                │
│  ┌─ left: voice ─────────┐  ┌─ right: conversation ─────────┐  │
│  │ <state pill>          │  │ <ActivityFeed>                │  │
│  │ <VoiceVisualizer>     │  │   you: hey friday what does…  │  │
│  │ <UserTranscriptLive>  │  │   friday: on it…              │  │
│  │ <ConnectButton>       │  │   ⎿ tool: read foo.py         │  │
│  │ <MicMuteButton>       │  │   ⎿ tool: grep useEffect      │  │
│  └───────────────────────┘  │   friday: this hook…          │  │
│                             └───────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

Existing `<TransportStatePill>` stays at the top of the left column;
worth keeping our own because it reads `usePipecatClientTransportState()`
directly with strings we control.

### 5. Posture on `voice-ui-kit`

Use kit components by default. Replace one-by-one only if a specific
component breaks against `WebSocketTransport`. The earlier "build our
own" framing was an overcorrection; the kit's component layer is
transport-agnostic, only the app shell isn't.

If the kit's component takes a `transport` or `client` prop and that
prop's typing rejects our setup, that's the signal to either (a) cast
through if the runtime behavior is fine, or (b) drop in a small
replacement. Don't fork the kit.

---

## File-level checklist

- `server/friday/voice/pipecat_adapter.py` — emit RTVI server messages
  for opencode events. Add the small mapping helper.
- `server/tests/test_pipecat_adapter.py` — assert RTVI frames produced
  for a fake event stream.
- `web/src/pages/VoiceRoom.tsx` — replace hand-rolled controls with kit
  components, add two-column layout, mount `<ActivityFeed>`.
- `web/src/components/ActivityFeed.tsx` (new) — subscribes to RTVI events
  via `useRTVIClientEvent`, renders chronological feed.
- `web/src/components/UserTranscriptLive.tsx` (new, ~30 lines, only if
  the kit's transcript component doesn't slot in cleanly) — renders the
  most recent `user-transcription` event.
- No changes to: `friday/voice/server.py`, REST/SSE endpoints,
  `SessionsList.tsx`, `SessionView.tsx`.

---

## Verification

After landing the changes, the voice room should:

- Show a live mic-level wave when you speak (not after).
- Show your words appearing letter-by-letter as you speak (interim
  transcripts), then locking into a finalized line at end-of-utterance.
- Show "running grep" / "reading foo.py" in the activity feed *while*
  opencode runs tools, not at the end.
- Show streaming assistant text as it arrives (before TTS finishes
  speaking).
- Visualizer should pulse during TTS playback.

Probe script (`web/scripts/probe-voice.mjs`) gets one extension: count
RTVI events received per type during a fake-audio run. Fail the probe if
zero `user-transcription` interims arrive — that catches the "live
partial broken" regression cheaply.

`make check` (60 backend tests + lint) must stay green.

---

## Out of scope

- Markdown-stripping for TTS. Separate task.
- Push-to-talk vs hands-free toggle. Kit primitives exist; wire after
  this lands.
- Auth (Step 6 in jarvis.md). Independent track.
- Connect-latency optimization beyond current 1 s. Not needed.
