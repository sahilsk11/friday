# OpenCode Voice Gateway (OpenCode + ElevenLabs, TS/React)

This document describes a browser–backend architecture for a low‑latency voice interface on top of **OpenCode** and **ElevenLabs**. It is written as an implementation spec to hand to a coding agent: the goal is not to write the code here but to capture all integration details and contracts clearly.[web:21][web:25][web:97][web:100]

The current scope is **OpenCode only**, but the API contracts and abstractions are designed so that other coding agents (Hermes, Claude Code, etc.) can be added later without changing the frontend.

---

## 1. Frontend overview

The frontend is a single‑page web app built with **React + TypeScript**. It never talks directly to OpenCode or ElevenLabs. Instead, it communicates with a local **voice gateway** backend via one persistent WebSocket connection.

### Responsibilities

- Capture microphone audio.
- Stream audio chunks to the gateway.
- Display partial and final STT transcripts.
- Show OpenCode session status and tool activity.
- Render streaming agent text as it arrives.
- Play streaming TTS audio and support interruption.
- Provide basic controls: push‑to‑talk, hands‑free toggle, stop speaking, cancel active run.

### Minimal UI structure

- **Header**: session selector, connection status, basic config (voice, auto‑speak).
- **Main pane**: scrolling transcript/chat area showing:
  - user utterances (from STT or typed),
  - OpenCode agent responses,
  - tool/status messages (optionally compact).
- **Footer controls**:
  - push‑to‑talk button,
  - hands‑free toggle,
  - stop speaking button,
  - cancel active run button.

The frontend is driven entirely by events received from the backend; it does not make assumptions about OpenCode internals.

---

## 2. Frontend ↔ Backend API (WebSocket)

The WebSocket protocol is the **contract the frontend consumes**. It must not leak OpenCode‑specific or ElevenLabs‑specific details; instead, it uses a normalized event model that other backends can adopt later.

All messages are JSON objects with a `type` field. Audio payloads are sent as base64 for simplicity in v1; a later optimization can switch to binary frames.

### 2.1 Client → Gateway messages

```ts
// Sent from browser to voice gateway
export type ClientMessage =
  | {
      type: 'session.create';
      // Optional: hint for gateway to name or configure the session
      title?: string;
    }
  | {
      type: 'session.resume';
      sessionId: string;
    }
  | {
      type: 'audio.start';
      sessionId: string;
      // STT provider identifier, for now always 'elevenlabs' but keep as enum for future providers
      sttProvider?: 'elevenlabs';
      sampleRate: number; // e.g. 16000 or 44100
      encoding: 'pcm16'; // initial assumption; gateway may transcode if needed
      language?: string; // optional language hint
    }
  | {
      type: 'audio.chunk';
      sessionId: string;
      chunkBase64: string; // raw PCM16 or Opus encoded audio frame
      sequence: number; // strictly increasing per audio session
    }
  | {
      type: 'audio.stop';
      sessionId: string;
    }
  | {
      // Explicitly send a text turn (e.g. from a text input or finalized STT)
      type: 'turn.send';
      sessionId: string;
      text: string;
      // Where this turn came from
      source: 'typed' | 'stt-final';
    }
  | {
      // Cancel active work in the agent backend
      type: 'run.cancel';
      sessionId: string;
      // Optional turn/run identifier for more precise cancellation
      turnId?: string;
    }
  | {
      // Stop current TTS playback (but do not cancel the agent run)
      type: 'tts.stop';
      sessionId: string;
    }
  | {
      type: 'config.update';
      sessionId?: string;
      config: Partial<RuntimeConfig>;
    }
  | {
      type: 'ping';
      ts: number;
    };
```

### 2.2 Backend → Client messages

```ts
// Sent from voice gateway to browser
export type ServerMessage =
  | {
      type: 'session.created';
      sessionId: string;
      title?: string;
    }
  | {
      type: 'session.resumed';
      sessionId: string;
    }
  | {
      // High-level UI state for a session
      type: 'session.state';
      sessionId: string;
      state:
        | 'idle'        // no active run, no TTS
        | 'listening'   // mic open, audio streaming
        | 'transcribing'// STT in progress
        | 'running'     // agent backend (OpenCode) is working
        | 'speaking'    // TTS actively playing
        | 'error';
    }
  | {
      type: 'stt.partial';
      sessionId: string;
      text: string;
    }
  | {
      type: 'stt.final';
      sessionId: string;
      text: string;
    }
  | {
      // A turn was accepted by the backend; queued indicates whether it will
      // start immediately or after the current run finishes
      type: 'turn.accepted';
      sessionId: string;
      turnId: string;
      queued: boolean;
    }
  | {
      // Streaming text from the agent backend
      type: 'agent.text.delta';
      sessionId: string;
      turnId: string;
      text: string; // append to prior content for this turn
    }
  | {
      type: 'agent.text.final';
      sessionId: string;
      turnId: string;
      text: string; // final consolidated content for the turn
    }
  | {
      // Coarse status updates for the agent backend
      type: 'agent.status';
      sessionId: string;
      turnId?: string;
      status: 'thinking' | 'tool_running' | 'idle' | 'done';
      message?: string; // optional human-readable status
    }
  | {
      // Tool lifecycle notification, normalized across backends
      type: 'agent.tool';
      sessionId: string;
      turnId?: string;
      phase: 'start' | 'update' | 'end';
      toolName: string;
      message?: string; // optional short description/log line
    }
  | {
      type: 'tts.started';
      sessionId: string;
      turnId: string;
    }
  | {
      // Streaming audio from the TTS provider
      type: 'tts.audio.chunk';
      sessionId: string;
      turnId: string;
      sequence: number;
      audioBase64: string; // e.g. mp3 or PCM
      mimeType: 'audio/mpeg' | 'audio/pcm';
    }
  | {
      type: 'tts.ended';
      sessionId: string;
      turnId: string;
    }
  | {
      type: 'run.cancelled';
      sessionId: string;
      turnId?: string;
    }
  | {
      type: 'error';
      sessionId?: string;
      code: string;
      message: string;
      retryable?: boolean;
    }
  | {
      type: 'pong';
      ts: number;
    };
```

### 2.3 Runtime configuration shape

```ts
export type RuntimeConfig = {
  sttProvider: 'elevenlabs'; // future: 'deepgram' | 'whisper-local' | ...
  ttsProvider: 'elevenlabs';
  ttsVoiceId: string;
  ttsModelId: string;
  language?: string;
  autoSpeak: boolean; // whether to automatically speak agent replies
  autoSendFinalTranscript: boolean; // whether to automatically send final STT as a turn
  chunking: {
    maxChars: number;      // e.g. 200
    maxDelayMs: number;    // e.g. 200–300ms
    sentenceBoundary: boolean; // if true, prefer sentence boundaries
  };
};
```

---

## 3. Backend architecture (TypeScript/Node)

The backend (voice gateway) is a Node.js process written in TypeScript. It sits between the browser, OpenCode, and ElevenLabs, and implements all provider‑specific logic.

### 3.1 High‑level responsibilities

- Manage WebSocket connections from browsers.
- Manage one or more **OpenCode sessions** via the OpenCode JS SDK and HTTP APIs.[web:21][web:25]
- Manage **STT streams** to ElevenLabs (WebSocket realtime STT).[web:97][web:103][web:100]
- Manage **TTS streams** to ElevenLabs (WebSocket streaming TTS).[web:100][web:182]
- Maintain per‑session state machines: idle vs running vs speaking, plus queued turns.
- Normalize OpenCode events and tool output into the generic `ServerMessage` format.
- Implement a speaking policy and chunking strategy for TTS.

### 3.2 Project layout (backend)

A suggested structure:

```txt
voice-gateway/
  src/
    index.ts          // process entrypoint (HTTP + WS server)
    wsServer.ts       // WebSocket upgrade + routing
    sessionManager.ts // per-session state + queues
    agent/
      opencodeAdapter.ts
      types.ts
    stt/
      elevenLabsSttAdapter.ts
    tts/
      elevenLabsTtsAdapter.ts
    pipelines/
      ttsChunker.ts
      speakingPolicy.ts
    config.ts
    logger.ts
  package.json
  tsconfig.json
```

This is meant to be extensible: `agent/` can later contain `hermesAdapter.ts`, `claudeAdapter.ts`, etc., all implementing the same internal interface.

---

## 4. OpenCode integration (backend)

This is the section that matters most for an implementation agent that is not familiar with OpenCode internals.

### 4.1 OpenCode server model

OpenCode is a coding agent that runs as a **server + multiple clients** architecture. The `opencode serve` command runs a headless HTTP server that exposes an OpenAPI 3.1 endpoint and multiple REST APIs for sessions, messages, tools, files, and a **Server-Sent Events (SSE) stream** at `/event` and `/global/event`.[web:21]

Key facts:

- `opencode serve` defaults to `127.0.0.1:4096`, but hostname/port can be configured.[web:21]
- It can be protected by HTTP basic auth via `OPENCODE_SERVER_USERNAME` and `OPENCODE_SERVER_PASSWORD`.[web:21]
- The server exposes `/doc` for an OpenAPI 3.1 spec; this spec is used to generate the **JS/TS SDK**.
- Clients (TUI, IDE plugins, web) talk to the server, *not* directly to the underlying model.

For this project, the gateway is a **client** of the OpenCode server.

### 4.2 OpenCode JS/TS SDK

OpenCode provides a **type-safe JS/TS SDK** (`opencode-sdk-js`) that wraps all server APIs with typed methods.[web:25][web:172]

Important SDK capabilities:[web:25]

- `session` namespace:
  - `session.list()` – list sessions.
  - `session.create({ body })` – create a new session.
  - `session.get({ path })` – get a specific session.
  - `session.update({ path, body })` – update session metadata.
  - `session.abort({ path })` – abort a running session.
  - `session.messages({ path })` – list messages in a session.
  - `session.prompt({ path, body })` – send a prompt to a session and get an assistant response (synchronous).
- `event.subscribe()` – subscribe to a **server-sent events** stream.[web:25]
- `file`, `find`, `tui`, `auth`, etc. – for workspace operations and TUI control.

The SDK supports two main usage patterns:

- "full" instance: starts a server+client pair (not needed here; we already run `opencode serve`).
- client-only: `createClient({ baseUrl })` to connect to an existing server.[web:25]

For this project, the gateway should use **client-only mode** with `baseUrl` pointing at your existing OpenCode server (e.g., `http://127.0.0.1:4096`).[web:25][web:21]

### 4.3 Relevant HTTP/SSE endpoints

From the server docs:[web:21]

- **Sessions**
  - `POST /session` – create session.
  - `GET /session` – list sessions.
  - `GET /session/:id` – get session details.
  - `POST /session/:id/abort` – abort running session.
  - `POST /session/:id/fork` – fork session.
  - `POST /session/:id/share` / `DELETE /session/:id/share` – sharing.
  - `GET /session/:id/message` – list messages.
  - `POST /session/:id/message` – send a message and wait for response (synchronous).
  - `POST /session/:id/prompt_async` – send a message asynchronously, no wait (204 No Content).[web:21]

- **Events**
  - `GET /event` – server-sent events stream; first event is `server.connected`, then a bus of server events (session updates, logs, etc.).[web:21]
  - `GET /global/event` – global events (SSE) for health/logging; useful but less session-specific.[web:21]

Other endpoints (files, tools, etc.) are useful but optional for the first voice MVP.

The SDK’s `event.subscribe()` method wraps `/event` and returns rich event objects, which is preferred over hand-parsing raw SSE.[web:25]

### 4.4 Agent adapter interface

Define an internal interface that abstracts "whatever coding agent backend we are talking to". For now, implement it using OpenCode; later, other backends can implement the same interface.

```ts
// src/agent/types.ts

export interface AgentAdapter {
  createSession(input?: { title?: string }): Promise<{ sessionId: string }>;

  resumeSession(sessionId: string): Promise<{ sessionId: string }>;

  // Send a human turn to the backend. This should start work immediately if
  // the backend session is idle, or queue internally if the backend does
  // not support concurrent runs. The adapter returns a logical turnId.
  sendTurn(sessionId: string, text: string): Promise<{ turnId: string }>;

  // Cancel a running turn (best-effort). If turnId is omitted, cancel the
  // current active run for the session.
  cancelTurn(sessionId: string, turnId?: string): Promise<void>;

  // Subscribe to streaming events for a given session. Returns an unsubscribe
  // function. The adapter is responsible for mapping backend events into the
  // normalized handlers.
  subscribe(
    sessionId: string,
    handlers: {
      onTextDelta(text: string, turnId?: string): void;
      onTextFinal?(text: string, turnId?: string): void;
      onToolEvent?(event: {
        phase: 'start' | 'update' | 'end';
        toolName: string;
        message?: string;
      }): void;
      onState?(state: 'running' | 'idle' | 'done'): void;
      onError?(error: Error): void;
    }
  ): Promise<() => Promise<void>>;
}
```

### 4.5 OpenCode adapter implementation plan

Implement `AgentAdapter` in `opencodeAdapter.ts` using `opencode-sdk-js` in client-only mode.[web:25][web:172]

#### 4.5.1 Creating and resuming sessions

- Use `session.create({ body: { title? } })` to create a new session.
- Use `session.get({ path: { id } })` or `session.list()` to resume an existing session by ID.

Example shape:

```ts
import { createClient } from '@anomalyco/opencode-sdk-js';

const client = createClient({ baseUrl: 'http://127.0.0.1:4096' });

export async function createSession(input?: { title?: string }) {
  const session = await client.session.create({ body: { title: input?.title } });
  return { sessionId: session.id };
}
```

The adapter should keep an internal map from `sessionId` to any local state it needs (e.g. last message ID if that’s useful).

#### 4.5.2 Sending turns

There are two main choices:

1. **Synchronous prompt** (`session.prompt({ path, body })`)
   - Sends a prompt and waits for the full assistant response.
   - Simpler but higher latency; bad for streaming.

2. **Asynchronous prompt** (`POST /session/:id/prompt_async`)
   - Sends a message and returns immediately (204 No Content).
   - The actual work is reflected via events on `/event`.

For a streaming voice UX, prefer **asynchronous prompt** and rely on the SSE event stream (via `event.subscribe()` in the SDK) for incremental state.[web:21][web:25]

Implementation sketch:

```ts
export async function sendTurn(sessionId: string, text: string) {
  // Generate a logical turnId; this may be distinct from OpenCode message IDs
  const turnId = generateTurnId();

  await client.session.promptAsync({
    path: { id: sessionId },
    body: {
      // noReply: false so we actually get an assistant turn
      parts: [{ type: 'text', text }],
    },
  });

  return { turnId };
}
```

Behind the scenes, OpenCode will:
- enqueue the user message,
- run the agent/model/tools against your workspace,
- emit events on `/event` as work progresses.

#### 4.5.3 Subscribing to events

The SDK exposes `event.subscribe()` for a server-sent events stream of structured events.[web:25][web:21]

Basic behavior:

- First event is `server.connected`.
- Subsequent events include a `type` or `event` field and a payload describing session changes, messages, tool activity, permission prompts, etc.[web:21]

The adapter should:

- Subscribe once per gateway instance (or per backend connection) and demultiplex events by `sessionId`.
- For each event:
  - If it is a **message delta / token chunk** type (depends on OpenCode’s exact event schema), call `handlers.onTextDelta`.
  - If it represents a completed assistant message, call `handlers.onTextFinal`.
  - If it represents a tool invocation, call `handlers.onToolEvent` with `phase` mapped to `start/update/end`.
  - If it indicates that the session has become idle or completed, call `handlers.onState('idle' | 'done')`.

Because the exact event names/types are defined in the OpenAPI and SDK type definitions, the implementation agent must inspect the `event.subscribe()` typings and payloads (e.g. `Event` types from the SDK) and map them to the generic handler calls.

Key goal: **do not** forward raw OpenCode events to the frontend. Normalize them into the `ServerMessage` shapes.

#### 4.5.4 Cancellation

Use `session.abort({ path: { id: sessionId } })` to abort a running session.[web:25][web:21]

The adapter should:

- Map `run.cancel` from the frontend to `session.abort`.
- After abort, expect events reflecting the aborted state; call `handlers.onState('idle')` and emit a `run.cancelled` message to the frontend.

#### 4.5.5 Session state machine

The adapter should not try to guess everything from events; instead, pair event handling with a simple state machine in `sessionManager.ts`:

States per session:

- `idle`: no run in progress, no TTS.
- `running`: OpenCode is actively executing tools/model.
- `speaking`: TTS is playing.

Transitions:

- When `sendTurn` is called and no run is active → `running`.
- When events indicate work started (e.g. a new assistant message or tool activity) → `running`.
- When events indicate work completed or aborted → `idle`.
- When TTS starts → `speaking`.
- When TTS ends or is stopped → back to `idle` (or `running` if work continues).

The session manager is then responsible for implementing the behavior you want:

- If a final STT transcript arrives while state is `idle` → call `sendTurn` immediately and mark `state = 'running'`.
- If it arrives while state is `running` or `speaking` → push into a per-session **queue**.
- When state transitions from `running` → `idle` → dequeue the next queued turn and send it.

This design explicitly avoids the "hook must fire" deadlock you saw with the MCP-based solution.

---

## 5. ElevenLabs integration (STT + TTS)

ElevenLabs will be used for both realtime **speech-to-text** and **text-to-speech**.

### 5.1 STT adapter

Use the ElevenLabs **Realtime Speech-to-Text** WebSocket API (`/v1/speech-to-text/realtime`). It accepts audio frames and returns partial and final transcripts.[web:97][web:103]

Define an internal interface:

```ts
// src/stt/elevenLabsSttAdapter.ts

export interface SttAdapter {
  start(options: {
    sessionId: string;
    language?: string;
    onPartial(text: string): void;
    onFinal(text: string): void;
    onError(error: Error): void;
  }): Promise<void>;

  sendAudio(chunk: ArrayBuffer): void;

  stop(): Promise<void>;
}
```

Implementation notes:

- On `audio.start`, create/open an ElevenLabs STT WebSocket connection.
- For each `audio.chunk`, forward the decoded bytes to ElevenLabs.
- On ElevenLabs partial transcript messages, call `onPartial` and emit `stt.partial` to frontend.
- On final transcript messages, call `onFinal` and emit `stt.final`. If `autoSendFinalTranscript` is true in config, also trigger `sendTurn` logic.

### 5.2 TTS adapter

Use the ElevenLabs **streaming TTS** WebSocket endpoint (`/v1/text-to-speech/{voice_id}/stream-input`). It allows streaming text input and returns audio chunks incrementally.[web:100][web:182][web:85][web:88]

Define an internal interface:

```ts
// src/tts/elevenLabsTtsAdapter.ts

export interface TtsAdapter {
  start(options: {
    sessionId: string;
    turnId: string;
    voiceId: string;
    modelId: string;
    onChunk(chunk: Uint8Array, sequence: number): void;
    onStart(): void;
    onEnd(): void;
    onError(error: Error): void;
  }): Promise<void>;

  // Send a piece of text to be spoken. The adapter can buffer/merge inputs
  // if needed; upstream logic should already apply chunking heuristics.
  sendText(text: string): void;

  // Flush any buffered text to ensure it is spoken.
  flush(): void;

  // Stop speaking and close the WebSocket.
  stop(): Promise<void>;
}
```

Implementation notes:[web:100][web:182]

- When the first `agent.text.delta` suitable for speaking arrives (per speaking policy), call `start`, which opens an ElevenLabs TTS WebSocket.
- Use `ttsChunker` to buffer incoming text deltas and call `sendText` on sentence boundaries or after `maxChars` / `maxDelayMs`.
- For each audio chunk from ElevenLabs, invoke `onChunk` and emit `tts.audio.chunk` to the frontend.
- On WebSocket close, call `onEnd` and emit `tts.ended`.
- On `tts.stop` from the frontend, call `stop` and cease sending audio chunks.

---

## 6. Session manager & speaking policy

The **session manager** coordinates STT, agent backend (OpenCode), and TTS.

### 6.1 Responsibilities

- Track per-session state (`idle`, `running`, `speaking`).
- Maintain a queue of pending turns per session.
- Decide when to call `agentAdapter.sendTurn`.
- Decide when to start and stop TTS per session.
- Apply a speaking policy so not every token/tool log is spoken.

### 6.2 Speaking policy

Guidelines:

- Speak only assistant text that is intended for the user.
- Do **not** speak:
  - raw tool traces,
  - shell commands or large code blocks,
  - stack traces.
- Optionally:
  - If a response exceeds a configured length, summarize before speaking.

Implementation approach:

- In the OpenCode adapter’s `onTextDelta`, tag deltas with metadata (e.g. channel: 'assistant' | 'tool' | 'system').
- In `speakingPolicy`, filter deltas by channel and heuristics (e.g. presence of Markdown code fences, log prefixes).
- Only forward filtered text to `ttsChunker`.

### 6.3 Turn scheduling

Key behavior (fixing the "pending" issue from hook-based solutions):

- When a **final STT transcript** arrives:
  - If `state === 'idle'`: call `sendTurn` immediately and mark `state = 'running'`.
  - If `state === 'running'` or `state === 'speaking'`: push the transcript text into a per-session FIFO queue.

- When OpenCode events indicate a run has finished (no more tools, no more assistant text):
  - Mark `state = 'idle'`.
  - If the queue is non-empty, dequeue the next text and call `sendTurn`. Mark `state = 'running'` again.

- When TTS starts for a turn: temporarily set `state = 'speaking'` (or maintain both an `agentState` and `ttsState` if you prefer orthogonal flags).

This state machine lives in `sessionManager.ts` and is the primary guard against idle deadlocks.

---

## 7. Project architecture summary

### 7.1 Processes

- **OpenCode server**
  - Started via `opencode serve` on your machine or server.
  - Exposes HTTP API and `/event` SSE stream. [web:21]

- **Voice gateway (Node/TS)**
  - Connects to OpenCode via JS SDK & `/event`.
  - Connects to ElevenLabs STT and TTS via WebSockets.[web:25][web:100]
  - Hosts WebSocket endpoint for frontend.
  - Optionally serves the static frontend assets.

- **Frontend (React/TS)**
  - Single-page app.
  - Talks only to gateway WebSocket.

### 7.2 Extensibility for future backends

To support Hermes, Claude Code, etc. later:

- Keep `AgentAdapter` as the only interface between gateway and coding agents.
- Implement `hermesAdapter`, `claudeAdapter`, etc. with their own event subscriptions and `sendTurn` logic.
- Do **not** change the WebSocket protocol consumed by the frontend.

This ensures that adding a new backend is a backend-only change.

---

## 8. Implementation notes for coding agent

For whichever coding agent will implement this spec:

1. Use **TypeScript** for both the frontend and backend.
2. On the backend, install and use the OpenCode JS SDK in client-only mode against an existing `opencode serve` instance.[web:25][web:21]
3. Implement `AgentAdapter` using `session.create`, `session.prompt_async` (or equivalent SDK helper), `session.abort`, and `event.subscribe`.
4. Normalize OpenCode events into the `ServerMessage` event shapes described above.
5. Integrate ElevenLabs realtime STT and streaming TTS over WebSocket per their docs.[web:97][web:103][web:100][web:182]
6. Implement the WebSocket server that speaks the `ClientMessage` / `ServerMessage` protocol.
7. Implement the session manager and turn queue to avoid deadlocks when idle.
8. Build a minimal React client that connects to the gateway, sends `session.create`, `audio.start`/`audio.stop`/`audio.chunk`, and renders events.

With this, the agent has enough context about OpenCode’s server and SDK APIs, ElevenLabs realtime APIs, and the desired UX to build the full system without needing further research.[web:21][web:25][web:97][web:100]
