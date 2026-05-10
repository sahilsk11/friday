# Friday Conversation Tool

Drives a real Friday voice session through a browser so a coding agent can use the product without a human at the keyboard.

## Agent Quick Start

Use two terminals.

Terminal 1, from the repo root:

```bash
./start.sh
```

Wait until it prints both URLs:

```text
BE  -> http://localhost:8000
FE  -> http://localhost:5173
```

Terminal 2, from `web/`:

```
npm run friday:conversation -- --task "Friday, can you hear me? Please reply with exactly one short sentence." --headless --wait-after-send-ms 120000
```

Success means the command prints JSON with `"ok": true`. Key fields:

| Field | Meaning |
|------|---------|
| `ok` | Overall pass/fail |
| `okReason` | Human-readable explanation |
| `heardUser` | User speech was detected |
| `assistantResponded` | Assistant produced a response |
| `hasErrors` | Error entries found in feed |
| `sessionId` | Friday/OpenCode session created or reused |
| `artifactsDir` | Screenshots, audio, and summaries for the run |
| `tracePath` | Full timeline for debugging |
| `finalUi.feed` | Transcript/activity visible in the UI |

Do not run `opencode serve` manually unless `./start.sh` says nothing is listening on `:4096`. `./start.sh` handles OpenCode correctly.

## Prerequisites

Run Friday's local backend and frontend from this checkout:

```bash
./start.sh
```

`./start.sh` always starts Friday's local FastAPI backend on `:8000` and Vite frontend on `:5173`, so local changes are included in tests.

For OpenCode on `:4096`, `./start.sh` reuses an existing server if one is already listening. If not, it starts a local `opencode serve --hostname 127.0.0.1 --port 4096` process and shuts down only that process on exit. On the sas box, this normally means reusing `opencode-serve-sas.service`.

The script needs Playwright's Chromium browser and `ffmpeg`. On macOS it uses built-in `say`; on Linux it uses FFmpeg's `flite` filter.

If Playwright's browser cache is missing:

```bash
npx playwright install chromium
```

## Single Turn

```bash
npm run friday:conversation -- --task "your message to Friday" --headless
```

Defaults: `opencode` harness, `opencode/minimax-m2.5-free` model, repo root directory.

For slow models or first runs, prefer:

```bash
npm run friday:conversation -- --task "your message to Friday" --headless --wait-after-send-ms 120000
```

## Multi-Turn

First turn creates a session. Copy the `sessionId` from the output and pass it to subsequent turns:

```bash
npm run friday:conversation -- --task "first message" --headless
# -> sessionId: ses_abc123

npm run friday:conversation -- --task "follow-up question" --existing-session ses_abc123 --headless
```

The session preserves conversation history across turns.

## CLI Flags

| Flag | Default | Notes |
|------|---------|-------|
| `--task` | `"Friday, can you hear me?..."` | What to say |
| `--harness` | `opencode` | `opencode`, `claudecode`, `codex` |
| `--directory` | repo root | Project directory |
| `--title` | auto-generated | Session title |
| `--model` | `opencode/minimax-m2.5-free` | Provider/model ID |
| `--headless` | `false` | Run without a visible browser window |
| `--existing-session` | — | Session ID from a prior run |
| `--output` | `artifacts/friday-conversations/` | Root directory for artifacts |
| `--fe-base-url` | `http://localhost:5173` | Frontend URL |
| `--leading-silence-seconds` | `8` | Silence before speech in generated audio |
| `--wait-after-start-ms` | `15000` | How long to wait after clicking Start |
| `--wait-after-send-ms` | `45000` | How long to wait for a response after Send |
| `--cdp-port` | `9234` | Chrome DevTools Protocol port |

## Artifacts

Each run creates a timestamped directory:

```
artifacts/friday-conversations/<timestamp-title>/
  summary.json       — top-level pass/fail, session ID, final UI state
  timeline.jsonl     — full event log (see below)
  input.wav          — generated speech audio
  input.aiff         — intermediate say output on macOS
  input.txt          — intermediate Linux TTS input text
  speech.wav         — intermediate ffmpeg output
  screenshots/       — home, modal, before/after record, before send, final
```

### Reading timeline.jsonl

Newline-delimited JSON. Each row has `tMs` (ms since run start), `wallTime`, and `event`.

Key event types:

| Event | Meaning |
|-------|---------|
| `browser-console` | Console log from the page |
| `rtvi-message` | RTVI protocol message (user-started-speaking, user-stopped-speaking, bot-ready, server-message) |
| `bot-ready-console` | Pipecat client reports bot is ready |
| `websocket-open/close` | Voice WebSocket lifecycle |
| `websocket-frame-sent/received` | Frame counts and byte totals |
| `ui-change/ui-sample` | DOM activity feed snapshot |
| `browser-pageerror` | Uncaught page errors |
| `bot-audio` | Audio pipeline events (experimental) |
| `click-*` | Button interactions |
| `recording-auto-started` | Voice recording was already active after bot-ready |
| `click-send-skipped` | VAD already finalized the turn before the runner clicked Send |
| `speech-detected` | User speech was detected in the feed during recording |
| `no-speech-detected` | Recording was active but no speech event fired |
| `select-model-for-existing-session` | Model was changed for an existing session |
| `model-already-correct` | Requested model was already selected |
| `mic-confirmed-on` | Mic state confirmed active after clicking Start |
| `mic-still-off-after-start` | Mic remained off after clicking Start (investigate) |
| `screenshot` | Screenshot captured |
| `run-error` | Top-level failure |

To answer "did Send happen before or after VAD finalized the user turn?", grep for `user-stopped-speaking` vs `click-send-start` by `tMs`.

### Fast Artifact Check

Open `summary.json` first. If `ok` is `false`, check `okReason` and then inspect these in order:

| Check | What It Means |
|------|---------------|
| `error` exists | The runner failed before completing the browser flow |
| `heardUser: false` | Fake microphone audio was not captured or transcribed |
| `heardUser: true` but `assistantResponded: false` | Voice backend or LLM did not produce a response before timeout |
| `hasErrors: true` | Assistant returned an error (e.g. unsupported model) |
| `timeline.jsonl` has `browser-pageerror` | The frontend threw an exception |
| `timeline.jsonl` has no `bot-ready-console` | Pipecat/voice connection did not become ready |
| `timeline.jsonl` has `no-speech-detected` | Recording was active but no speech event fired |
| screenshots stop before `final` | The browser flow failed mid-run |

## Known Product Behaviors

- **VAD auto-dispatches before Send**: The voice path finalizes the user turn via VAD alone. `Send` often happens after the backend has already sent the turn to the model. The product is not in strict manual-commit mode.
- **Recording auto-starts after bot-ready**: The runner does not click `Start`; clicking can race the transport and throw `Already recording`.
- **"Client DISCONNECTED" while active**: The UI status shows `DISCONNECTED` even though the session is running. Cosmetic.
- **Reconnecting session auto-starts recording**: When re-entering an existing session, the bot can auto-start recording before the tool clicks Start. The script handles this by checking if recording is already active.
- **Model persistence**: The server derives `current_model` from the last assistant message in the transcript. The UI seeds from this on page load, falling back to the harness default if no transcript model is available. Client-side localStorage still applies for user overrides.
- **TTS/STT transcription degrades numbers and short phrases**: The Linux TTS path (flite) misrecognizes numeric prompts. Avoid arithmetic and exact-token tests for fake-mic smoke tests.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Executable doesn't exist ... chromium_headless_shell` | Run `npx playwright install chromium` from `web/` |
| `curl http://localhost:5173` fails | Start `./start.sh` from the repo root |
| `:4096` already in use | Usually fine. `./start.sh` reuses it and does not kill it |
| `ok: false` with only a `you...` feed item | Rerun with `--wait-after-send-ms 120000` and inspect OpenCode/backend logs if still failing |
| `run-error` before browser launch | Check `ffmpeg` is installed and Playwright Chromium is installed |
| Needs deployed single-origin Friday instead of local Vite | Pass `--fe-base-url http://localhost:8765` |

## Limitations

- **Fake mic audio is static**: `--use-file-for-fake-audio-capture` is set at Chromium launch and can't be swapped mid-session. Multi-turn currently requires relaunching the browser per turn (the script does this automatically — each invocation is a fresh browser).
- **Headless audio**: In headless mode, Chromium may process audio differently. Screenshots and feed text are reliable; audio playback verification is not.
- **Bot speaking detection**: No reliable browser-level signal for "TTS audio is playing." The transcript and RTVI messages are used as proxies.

## Future

- **Barge-in**: Feed a second utterance while the bot is responding, verify TTS stops and the new turn wins. Blocked on dynamic microphone input (virtual mic or injectable `MediaStream`).
- **Batch runs**: Run multiple conversation tasks sequentially or in parallel. A thin wrapper around the single-instance runner.
- **Long VAD timeout**: A backend knob to prevent auto-dispatch of the user turn, giving the agent time to click `Send` manually.
