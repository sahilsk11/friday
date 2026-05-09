# Friday Conversation Tool

Drives a real Friday voice session through a browser so a coding agent can use the product without a human at the keyboard.

```
npm run friday:conversation -- --task "Ask Friday to summarize the README"
```

## Prerequisites

`./start.sh` must be running in another shell:

```bash
./start.sh
```

The script needs `say` (macOS built-in) and `ffmpeg` (at `/opt/homebrew/bin/ffmpeg`).

## Single Turn

```bash
npm run friday:conversation -- --task "your message to Friday" --headless
```

Defaults: `opencode` harness, `opencode/minimax-m2.5-free` model, repo root directory.

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
  input.aiff         — intermediate say output
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
| `screenshot` | Screenshot captured |
| `run-error` | Top-level failure |

To answer "did Send happen before or after VAD finalized the user turn?", grep for `user-stopped-speaking` vs `click-send-start` by `tMs`.

## Known Product Behaviors

- **VAD auto-dispatches before Send**: The voice path finalizes the user turn via VAD alone. `Send` often happens after the backend has already sent the turn to the model. The product is not in strict manual-commit mode.
- **"Client DISCONNECTED" while active**: The UI status shows `DISCONNECTED` even though the session is running. Cosmetic.
- **Reconnecting session auto-starts recording**: When re-entering an existing session, the bot can auto-start recording before the tool clicks Start. The script handles this by checking if recording is already active.

## Limitations

- **Fake mic audio is static**: `--use-file-for-fake-audio-capture` is set at Chromium launch and can't be swapped mid-session. Multi-turn currently requires relaunching the browser per turn (the script does this automatically — each invocation is a fresh browser).
- **Headless audio**: In headless mode, Chromium may process audio differently. Screenshots and feed text are reliable; audio playback verification is not.
- **Bot speaking detection**: No reliable browser-level signal for "TTS audio is playing." The transcript and RTVI messages are used as proxies.

## Future

- **Barge-in**: Feed a second utterance while the bot is responding, verify TTS stops and the new turn wins. Blocked on dynamic microphone input (virtual mic or injectable `MediaStream`).
- **Batch runs**: Run multiple conversation tasks sequentially or in parallel. A thin wrapper around the single-instance runner.
- **Long VAD timeout**: A backend knob to prevent auto-dispatch of the user turn, giving the agent time to click `Send` manually.
