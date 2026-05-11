# Friday Self-Test

Drives the app through a real browser, LiveKit, the Friday agent, and the
selected provider harness. The default mode still exercises fake microphone
audio and STT. Text mode uses the composer and validates the same
narrator/provider path after transcription.

```bash
cd web
npm run friday:self-test -- --headless
```

For the faster text path:

```bash
cd web
npm run friday:text-test -- --headless
```

## Prerequisites

Run the normal local stack first:

```bash
make livekit
make api
make agent
make web
```

The script also needs:

- macOS `say`
- `ffmpeg`
- a Playwright Chromium install (`npx playwright install chromium` if needed)
- `ELEVEN_API_KEY` configured for realtime STT
- a working selected harness, defaulting to `opencode` with model `minimax-m2.5-free`

## What It Verifies

- FastAPI responds at `/healthz`.
- The browser can open the Friday frontend.
- The create-session UI can create a LiveKit room payload.
- The browser can join the room and publish fake microphone audio.
- The Friday agent joins, receives turn-control data messages, commits STT,
  sends the transcript to the provider harness, and publishes a `text_final`
  response back to the UI.
- In text mode, the browser submits one or more composer turns, the UI receives
  a `text_final` response, and the script verifies narrator events plus provider
  transcript state through the API.

## Useful Flags

```bash
npm run friday:self-test -- \
  --headless \
  --harness opencode \
  --model minimax-m2.5-free \
  --task "Friday, reply with exactly: self test passed."
```

```bash
npm run friday:text-test -- \
  --headless \
  --scenario multi-turn \
  --task "Reply with exactly: Text path works."
```

| Flag                        | Default                 |
| --------------------------- | ----------------------- |
| `--fe-base-url`             | `http://localhost:5173` |
| `--api-base-url`            | `http://localhost:8000` |
| `--harness`                 | `opencode`              |
| `--model`                   | `minimax-m2.5-free`     |
| `--mode`                    | `voice`                 |
| `--scenario`                | unset                   |
| `--turn`                    | repeatable text turns   |
| `--directory`               | repo root               |
| `--output`                  | `artifacts/self-tests`  |
| `--leading-silence-seconds` | `2`                     |
| `--hold-ms`                 | `10000`                 |
| `--wait-agent-ms`           | `30000`                 |
| `--wait-after-release-ms`   | `90000`                 |

Each run writes:

```text
artifacts/self-tests/<timestamp-title>/
  summary.json
  timeline.jsonl
  narrator-events.json
  session-detail.json
  input.wav
  speech.wav
  screenshots/
```

`input.wav` and `speech.wav` are only written in voice mode.
