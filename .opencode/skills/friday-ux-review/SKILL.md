---
name: friday-ux-review
description: Run voice conversations with Friday, observe the UX via screenshots and transcripts, find issues, and fix them.
---

## What this skill does

Run real browser-driven voice conversations with Friday, inspect the results,
and improve the application based on what you observe.

## Before you start

Read these two files first:

- `web/scripts/CONVERSATION-TOOL.md` — how the conversation tool works
- `AGENTS.md` — repo conventions

## Starting Friday locally

Run this from the repo root in the background:

```bash
nohup ./start.sh >/tmp/opencode/friday-start.log 2>&1 &
```

Wait for both `http://localhost:8000` and `http://localhost:5173` to come up.
Use `curl -s http://localhost:5173` to check.

`./start.sh` handles OpenCode on `:4096` —
it reuses an existing server or starts one.

If Playwright's Chromium is missing:

```bash
cd web && npx playwright install chromium
```

## Running a conversation

Always run from `web/`. The tool drives a headless browser that feeds
pre-generated speech audio through a fake microphone.

```bash
cd web
npm run friday:conversation -- \
  --task "your message to Friday" \
  --headless \
  --wait-after-send-ms 120000
```

The output is JSON. Success means `"ok": true`. Check `okReason`, `heardUser`,
`assistantResponded`, and `hasErrors` for details.

## Multi-turn conversations

Take the `sessionId` from the first run's output and use it:

```bash
npm run friday:conversation -- \
  --existing-session <sessionId> \
  --task "follow-up question" \
  --headless \
  --wait-after-send-ms 120000
```

Each turn relaunches the browser (fake mic is static per launch).

## Inspecting results

For each run, open these in order:

1. `summary.json` — top-level pass/fail, session ID, transcript
2. `screenshots/05-final.png` — final UI state
3. `timeline.jsonl` — only if something looks wrong

Look for `browser-pageerror` in the timeline. It is a real frontend error.

Expected trace events that are NOT failures:
- `recording-auto-started` — Friday auto-starts recording after bot-ready
- `click-send-skipped` — VAD finalized the turn before manual Send
- `speech-detected` — User speech was detected in the feed
- `model-already-correct` — Requested model was already selected
- `mic-confirmed-on` — Mic state confirmed active

## Known product behaviors

- Recording auto-starts after bot-ready. Do not click Start.
- VAD often commits the turn before Send is clickable.
- `Client DISCONNECTED` in the UI while the session is working is cosmetic.
- The model is seeded from the server's `current_model` (derived from the last assistant response) on page load, falling back to the harness default.
- TTS/STT on Linux degrades numbers and short phrases — avoid arithmetic prompts in smoke tests.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Executable doesn't exist ... chromium_headless_shell` | `npx playwright install chromium` from `web/` |
| `curl http://localhost:5173` fails | Start `./start.sh` |
| `:4096` already in use | Normal. The script reuses it. |
| `heardUser: false` | Fake mic audio not captured — check ffmpeg and Playwright |
| `assistantResponded: false` | Rerun with `--wait-after-send-ms 120000` |
| `hasErrors: true` | Check `okReason` and timeline for model/harness errors |
| `run-error` before browser launch | Check ffmpeg and Playwright Chromium |

## After running conversations

1. Summarize what Friday said in each turn.
2. Note any anomalies in the transcripts or screenshots.
3. Identify at least one concrete UX issue.
4. Propose a fix. Implement it if it is a small change.
5. Run the conversation again to verify the fix.
