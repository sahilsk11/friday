# Friday Logging And Observability Strategy

## Goal

Friday should emit enough structured logs for another agent to reconstruct a
failed user turn without rereading the whole codebase. The first implementation
pass should not try to log everything. It should make the high-value paths
traceable end to end:

- browser starts or submits a turn
- LiveKit agent receives the RPC and controls audio/STT
- backend accepts the narrator turn
- provider session receives the prompt and emits state, tool, delta, final, or
  error events
- narrator decides whether to publish speech/text
- LiveKit agent relays the resulting event back to the room

The current repo already has durable per-process log files under
`.friday/runs/<run-id>/` from `scripts/refresh-local.sh`, exposed through
`make logs`. It also has useful persisted domain events in SQLite
(`narrator_events`, `provider_events`, `narrator_turns`). The missing piece is a
consistent structured log contract and correlation IDs across these surfaces.

## Current State

- `scripts/refresh-local.sh` writes process logs for OpenCode, FastAPI, the
  LiveKit agent, and Vite under `.friday/runs/<run-id>/`, with compatibility
  symlinks under `.friday/logs/`.
- `agent/main.py` uses stdlib `logging.basicConfig(level=logging.INFO)` and
  plain text messages for key voice and narrator steps.
- `server/app/main.py`, `server/app/narrator.py`,
  `agent/core/opencode_provider.py`, `agent/core/codex_provider.py`, and the
  narrator LLM code use named stdlib loggers, mostly with text messages.
- `web/src/features/room/FridayRoom.tsx` logs some client-side voice activity
  with `console.info`, but browser console logs are not captured in
  `.friday/runs`.
- The backend stores durable operational events, but those records are domain
  state, not process diagnostics. They should stay, but they do not replace
  logs.

## Package Choice

Use `structlog` for Python application logs, bridged through stdlib logging.

Reasons:

- It supports structured key/value logging without giving up existing
  `logging.getLogger(...)` integration.
- It handles context binding cleanly, which matters for `session_id`,
  `turn_id`, `provider_session_id`, `room_name`, and `request_id`.
- It can render JSON in process log files while keeping readable local output if
  needed.
- It is small enough for this codebase and avoids introducing a full telemetry
  stack before the event model is stable.

Do not add OpenTelemetry in the first pass. The right next step is JSON logs
with stable fields. Traces can be layered on later once the IDs and event names
are working.

For the browser, do not add a logging package at first. Add a tiny local logger
wrapper that emits JSON to `console.info/error` and, in a later phase, can POST
important client events to a backend endpoint.

## Log Format

Every Python application log line should be a single JSON object. Use stable
snake_case keys. Do not bury structured data inside the message string.

Required fields:

```json
{
  "ts": "2026-05-11T04:22:10.123Z",
  "level": "info",
  "event": "voice.turn.end_requested",
  "logger": "friday.livekit.agent",
  "service": "livekit_agent",
  "run_id": "20260511T042210Z-12345"
}
```

Common correlation fields:

```json
{
  "request_id": "req_...",
  "session_id": "ses_...",
  "turn_id": "turn_...",
  "client_turn_id": "cturn_...",
  "provider_session_id": "ses_...",
  "provider_id": "opencode",
  "model_id": "opencode-go/deepseek-v4-flash",
  "room_name": "friday-ses_...",
  "participant_identity": "friday-user-ses_...",
  "rpc_method": "friday.turn.end",
  "source": "voice",
  "event_id": 123,
  "provider_event_id": 456,
  "duration_ms": 1842
}
```

Error fields:

```json
{
  "event": "provider.turn.send_failed",
  "level": "error",
  "error_type": "HTTPStatusError",
  "error_message": "502 Bad Gateway",
  "exception": "stack trace rendered by structlog"
}
```

Payload-size fields:

```json
{
  "text_len": 428,
  "text_hash": "sha256:...",
  "text_preview": "optional capped preview..."
}
```

Default rule: log text lengths and hashes, not full user/provider text. If a
human-readable preview is necessary for debugging, cap it to 200-300 characters
and make it opt-in or consistently redacted. Full text is already persisted in
the narrator store where appropriate.

## Event Naming

Use dot-separated event names with this shape:

`<area>.<entity>.<action>`

Examples:

- `app.startup.started`
- `app.startup.ready`
- `session.create.started`
- `session.create.completed`
- `voice.room.joined`
- `voice.agent.session_configured`
- `voice.rpc.received`
- `voice.turn.start_requested`
- `voice.turn.audio_enabled`
- `voice.turn.end_requested`
- `voice.turn.transcript_committed`
- `voice.turn.transcript_empty`
- `voice.turn.commit_failed`
- `voice.response.publish_started`
- `voice.response.published`
- `voice.response.publish_failed`
- `narrator.turn.received`
- `narrator.turn.created`
- `narrator.turn.sent_to_provider`
- `narrator.turn.provider_send_failed`
- `narrator.event.created`
- `narrator.event.relayed`
- `narrator.speech.decision`
- `narrator.speech.suppressed`
- `provider.session.create_started`
- `provider.session.ready`
- `provider.turn.send_started`
- `provider.turn.send_completed`
- `provider.sse.connected`
- `provider.sse.reconnecting`
- `provider.event.received`
- `provider.event.ignored`
- `provider.event.dispatched`
- `provider.final.received`
- `provider.error.received`
- `client.voice.start_requested`
- `client.voice.end_requested`
- `client.agent.dispatch_requested`
- `client.agent.join_timeout`

Keep `message` optional and human-readable. Agents should key off `event` and
fields, not parse prose.

## Correlation Model

The most important implementation detail is correlation.

Use these IDs:

- `run_id`: current local run. Source from `FRIDAY_RUN_ID`; if missing, set one
  during process startup. `refresh-local.sh` already creates a run ID.
- `request_id`: every FastAPI request. Add middleware that binds it to logs and
  returns it in `X-Request-ID`.
- `session_id`: Friday session ID, present on almost every meaningful event.
- `provider_session_id`: provider-owned session ID, especially for OpenCode and
  Codex.
- `client_turn_id`: generated by browser when a user starts/submits a turn.
  This should pass through LiveKit RPC and direct API text submissions.
- `turn_id`: backend durable narrator turn ID. The backend should include this
  in event payloads and API responses once created.
- `event_id`: narrator event ID or provider event ID when relaying existing
  persisted events.

The first pass should add `client_turn_id` to `TurnControlMessage`,
`NarratorTurnRequest`, and event payloads. `turn_id` can remain backend-owned,
but it must appear in logs and narrator events as soon as the backend creates
it.

## Core Flow Traces

### Text Turn

When one chunk of text comes in, the logs should show:

1. Browser logs `client.text.submit_requested` with `session_id`,
   `client_turn_id`, `mode`, `text_len`, and current connection state.
2. If using LiveKit, agent logs `voice.rpc.received` with `rpc_method`,
   `session_id`, `client_turn_id`, `participant_identity`, and `source=text`.
3. Agent logs `narrator.turn.submit_started` before POSTing to the backend.
4. FastAPI middleware logs the HTTP request start/end with `request_id`.
5. `NarratorManager.submit_user_turn` logs `narrator.turn.created` with
   `turn_id`, `client_turn_id`, `session_id`, `provider_session_id`,
   `provider_id`, `model_id`, and `text_len`.
6. Provider logs `provider.turn.send_started` and `provider.turn.send_completed`
   with duration.
7. Provider event ingestion logs state/tool/final/error events at useful
   boundaries, not every text delta.
8. Narrator final decision logs whether it spoke, fell back, timed out, or
   suppressed speech.
9. Agent logs every narrator event it relays back to LiveKit.
10. Browser logs receipt of `text_final`, `narration`, `error`, or state.

### Voice Turn

For a voice turn, the logs should show:

1. Browser logs `client.voice.start_requested`.
2. Agent logs `voice.turn.start_requested`, `voice.turn.audio_enabled`, and the
   participant binding.
3. Browser logs `client.voice.end_requested`.
4. Agent logs `voice.turn.end_requested` before disabling audio.
5. Agent logs `voice.turn.commit_started` with STT timeout and flush settings.
6. Agent logs exactly one terminal transcript event:
   `voice.turn.transcript_committed`, `voice.turn.transcript_empty`, or
   `voice.turn.commit_failed`.
7. If committed, the flow joins the text-turn path at
   `narrator.turn.submit_started`.

### OpenCode Session Ready

OpenCode readiness should be visible before a user turn depends on it:

1. `provider.sse.connecting` when `_run_sse_loop` attempts `/global/event`.
2. `provider.sse.connected` when the first SSE connection is established.
3. `provider.catalog.loaded` when models are loaded, with `model_count`.
4. `provider.session.create_started` and `provider.session.ready` around
   `/session`.
5. `provider.session.attached` when using an existing OpenCode session.
6. `provider.sse.reconnecting` with `generation`, `attempt`, and `delay_ms`
   when the loop fails.

### ElevenLabs Speech Not Published

The failure mode "ElevenLabs speech is not published" needs logs at two
separate layers:

1. Narrator/backend speech decision:
   - `narrator.speech.decision` with `decision_action`, `brain`,
     `decision_type`, `turn_id`, and duration.
   - `narrator.speech.suppressed` when no speech event is created, with
     `reason=silent_decision`, `reason=empty_text`, or `reason=fallback_silent`.
   - `narrator.event.created` when a `speech`, `progress`, or `final` event is
     persisted.
2. LiveKit agent publish/playback:
   - `_handle_narrator_event` should log `voice.narrator_event.handled` with
     `event_id`, `event_type`, `message_count`, `will_speak`, and
     `speaker_enabled`.
   - Before `session.say(...)`, log `voice.tts.say_started`.
   - If `session.say(...)` raises, log `voice.tts.say_failed` with exception.
   - If `_send_agent_response` publish fails, log
     `voice.response.publish_failed`.

This separates "the narrator chose not to speak" from "the backend created a
speech event but LiveKit/ElevenLabs failed to publish it."

## Where To Add Logs First

Implement in this order.

1. `friday/logging.py`
   - Configure `structlog`.
   - Read `FRIDAY_RUN_ID`, `FRIDAY_LOG_LEVEL`, and `FRIDAY_LOG_FORMAT`.
   - Add helpers for safe text metadata, duration timing, and context binding.

2. `server/app/main.py`
   - Add request middleware with `request_id`, route, method, status,
     duration_ms.
   - Log app startup/shutdown.
   - Log session create and voice-agent dispatch request/result.

3. `server/app/narrator.py`
   - Log `create_or_attach_session`, `_bind_provider`, `submit_user_turn`,
     `cancel`, recovery, provider event ingestion, final decision, speech
     creation/suppression, and progress scheduling.
   - This is the highest-value backend file because it owns durable turn IDs and
     bridges provider events to narrator events.

4. `agent/main.py`
   - Replace existing prose logs with structured events.
   - Add logs around LiveKit RPC parsing, command lock wait time, audio enable
     toggles, STT commit, transcript result, narrator API calls, event polling,
     event relay, `publish_data`, and `session.say`.

5. `agent/narrator_client.py`
   - Log backend HTTP method/path, status, duration, event count, and failures.

6. `agent/core/opencode_provider.py`
   - Log SSE connect/reconnect, session create/attach, provider turn send,
     provider state changes, final receipt, tool starts, ignored events, and
     provider errors.
   - Avoid logging every text delta at info level. Emit delta counters or debug
     logs only.

7. `agent/core/codex_provider.py`
   - Log subprocess spawn, model/directory, JSON event type counts, final
     emission, timeout/no-output, cancellation, process exit code, and stderr
     summaries.

8. `server/app/narrator_llm.py` and `server/app/narrator_brain.py`
   - Log LLM narrator decision start/end/failure with model/provider,
     duration_ms, action, and fallback reason. Do not log full prompts by
     default.

9. `web/src/features/room/FridayRoom.tsx`
   - Route existing `console.info` calls through a local `clientLogger`.
   - Include `session_id`, `room_name`, `participant_identity`,
     `client_turn_id`, connection state, pipeline stage, and relevant error
     messages.
   - Later, add a backend client-log endpoint so browser logs appear in
     `.friday/runs/current/client.log`.

## Noise Controls

Info level should include lifecycle boundaries and terminal outcomes. Debug
level should include high-frequency internals.

Use `info` for:

- process startup/shutdown
- room join/disconnect
- RPC received and completed
- turn created, sent, cancelled, completed, failed
- provider session ready
- provider state transitions
- tool start
- narrator final/progress/speech event created
- response published to browser

Use `warning` for:

- recoverable provider disconnect/reconnect
- narrator LLM fallback
- missing final recovery
- empty transcript
- dispatch retry or timeout
- dropped/ignored provider events that might explain missing output

Use `error` for:

- exceptions that fail a user turn
- STT commit failures
- backend narrator submission failures
- provider send failures
- TTS/publish failures
- startup failures

Use `debug` for:

- raw provider event names
- text delta counts
- polling with no new events
- JSON parse skips

Do not log per-poll "no events" at info level. Do not log every text delta at
info level. Aggregate counts and log the final boundary.

## Logs Versus Persisted Events

Keep persisted domain events for product state:

- narrator transcript
- provider events
- turn status
- final response recovery
- UI event replay

Use process logs for diagnostics:

- timing
- retries
- exceptions
- ignored events
- decisions not persisted because nothing was emitted
- transport failures
- request/RPC boundaries

If an event explains user-visible behavior, it probably belongs in the store.
If it explains why code made a decision or failed to do something, it belongs in
logs. Some high-value events, such as provider final receipt, should appear in
both.

## Implementation Notes

- Add `structlog` to `pyproject.toml`.
- Centralize configuration in one module and import it from both FastAPI and the
  LiveKit agent entrypoints.
- Prefer `logger = structlog.get_logger(__name__)` in changed files.
- Bind durable context once near boundaries:
  - FastAPI middleware binds request context.
  - `NarratorManager` binds session/turn/provider context.
  - LiveKit agent binds room/session/participant context for each RPC.
- Use `time.perf_counter()` for duration fields.
- Update `scripts/refresh-local.sh` to export `FRIDAY_RUN_ID="${RUN_ID}"` to
  every child process so logs match `run.json`.
- Consider adding `jq` examples to `scripts/show-current-logs.sh` once logs are
  JSON, for example filtering by `session_id` or `turn_id`.

## Acceptance Criteria

A logging implementation based on this doc is good enough when an agent can
answer these questions by reading `.friday/runs/current/*.log` and the narrator
store:

- Did the browser request a turn, and what `client_turn_id` was assigned?
- Did the LiveKit agent receive the RPC?
- Did the agent enable/disable audio at the expected time?
- Did STT produce a transcript, an empty result, or an exception?
- Did the backend create a durable `turn_id`?
- Which provider and model handled the turn?
- Was the provider session ready?
- Did the provider emit tool/state/final/error events?
- Did the narrator decide to speak or suppress speech, and why?
- Was a narrator event persisted?
- Did the LiveKit agent publish the event to the browser?
- Did `session.say(...)` run, fail, or get skipped due to speaker/session state?

If those questions are answerable without opening source files, the first
observability pass has succeeded.
