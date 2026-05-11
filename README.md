# Friday LiveKit PoC

Minimal greenfield LiveKit proof of concept for Friday voice sessions.

## Architecture

- LiveKit media server runs as a separate service.
- Friday FastAPI backend creates Friday sessions and LiveKit room tokens.
- Each Friday session maps to one LiveKit room.
- Browser joins the room with `livekit-client`, publishes microphone audio, and sends push-to-talk commands to the agent over LiveKit RPC.
- Python LiveKit agent joins each room through LiveKit Agents automatic dispatch.
- Agent uses ElevenLabs STT with provider-side VAD commits and logs Friday turn commits.
- FastAPI owns the narrator bridge, persists Friday's spoken transcript, forwards
  every user turn to the selected coding provider, and relays provider events
  back to the room for speech.

## Run

```bash
cp .env.example .env
docker compose up -d livekit
uv sync
uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
uv run python -m agent.main dev
```

In a third terminal:

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:5173`.

Set `ELEVEN_API_KEY` in `.env` before using transcription.

New-session model defaults live in
`server/app/harness_model_defaults.py`. Each harness can name a preferred model
with either a bare model id, such as `gpt-5.5`, or a provider-qualified OpenCode
ref, such as `opencode-go/deepseek-v4-flash`. Friday still returns the full live
provider catalog; the configured default only controls the initial selection in
the new-session modal.

By default the narrator sends every user turn to the coding provider without an
immediate canned acknowledgement, then asks an OpenRouter-compatible
chat-completions model whether to speak for progress/final events from
deterministic snapshots. The narrator model is prompted to return plain
conversational prose for TTS instead of copying Markdown, code blocks, command
output, or structured provider summaries. Set an OpenRouter key in `.env`:

```dotenv
FRIDAY_NARRATOR_BRAIN=openai_compatible
FRIDAY_NARRATOR_LLM_PROVIDER=openai_compatible
OPENROUTER_API_KEY=...
```

The default non-sensitive narrator settings are hard-coded as
`https://openrouter.ai/api/v1` and `openai/gpt-4o-mini`. If no narrator API
key is present, Friday falls back to the evented narrator: no progress chatter
and provider finals spoken as-is.

The same narrator brain can also use the running OpenCode server as its JSON
LLM backend:

```dotenv
FRIDAY_NARRATOR_BRAIN=openai_compatible
FRIDAY_NARRATOR_LLM_PROVIDER=opencode_server
FRIDAY_NARRATOR_OPENCODE_BASE_URL=http://127.0.0.1:4096
# Optional; omit to use OpenCode's default model.
FRIDAY_NARRATOR_OPENCODE_MODEL=provider/model
FRIDAY_NARRATOR_OPENCODE_AGENT=build
```

The OpenCode backend creates a short-lived session for each narrator decision,
asks for the same schema-shaped JSON object, denies discovered tools by default,
and deletes the temporary session after parsing the response.

To compare narrator LLM backends against the same sample snapshots:

```bash
uv run python scripts/probe-narrator-llms.py --trials 2
```

The probe auto-selects `opencode-go/deepseek-v4-flash` when that model is
available from the local OpenCode server.

## Production hosting

For the sas cutover, build the frontend and serve it from FastAPI rather than
running Vite in production:

```dotenv
FRIDAY_WEB_DIST=/home/sas/projects/friday/web/dist
FRIDAY_CORS_ORIGINS=https://friday.ultron.sh
```

Use separate LiveKit URLs in production. The backend and agent should talk to
LiveKit locally, while the browser receives the Cloudflare WSS hostname:

```dotenv
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_INTERNAL_URL=ws://127.0.0.1:7880
LIVEKIT_PUBLIC_URL=wss://friday-livekit.ultron.sh
FRIDAY_API_BASE_URL=http://127.0.0.1:8765
```

`friday-livekit.ultron.sh` must not sit behind the normal Cloudflare Access
login wall; LiveKit room JWTs authenticate room joins. See
`livekit.production.example.yaml` for the expected self-hosted LiveKit port
shape.

For stale local dev processes, use:

```bash
make refresh
```

That resets local API/agent/web processes and ensures LiveKit is running without
tearing down Docker. Use `make nuke` only when you want to stop LiveKit too.
The default API command intentionally runs without hot reload so code edits do
not interrupt live voice turns. Use `make api-reload` only when you explicitly
want FastAPI reload behavior.

Each `make refresh` run writes durable logs under `.friday/runs/<run-id>/` and
updates `.friday/runs/current` plus `.friday/logs/current` to point at the latest
run. The compatibility paths `.friday/logs/api.log`, `.friday/logs/agent.log`,
`.friday/logs/opencode.log`, and `.friday/logs/web.log` point at the current
run's logs. From any Codex session, use:

```bash
make logs
```

That prints the current run metadata and recent tails for OpenCode, FastAPI, the
LiveKit agent, and the frontend.

ElevenLabs realtime STT is configured with provider-side VAD by default:

```dotenv
ELEVENLABS_VAD_SILENCE_THRESHOLD_SECS=0.3
ELEVENLABS_MIN_SILENCE_DURATION_MS=300
FRIDAY_COMMIT_STT_FLUSH_DURATION_SECS=0.3
```

That keeps ElevenLabs committing transcript segments on short pauses instead of
waiting for its long automatic commit path. Friday still treats the user turn as
complete only when the browser sends `end_turn`.

For Docker-based local development, `docker-compose.yml` starts LiveKit with
`--node-ip 127.0.0.1` so same-host browser clients receive reachable ICE
candidates instead of the container bridge IP.

## Checks

```bash
uv run mypy
cd web && npm run typecheck && npm run build
```

## Self-Test

With LiveKit, the API, the agent, and the web dev server running, the app can
drive a real voice turn through Playwright:

```bash
cd web
npm run friday:self-test -- --headless
```

From the repo root, the same check is available as:

```bash
make self-test
```

The runner generates speech with `say`, feeds it to Chromium as a fake
microphone, holds the room's push-to-talk button, and waits for a provider
`text_final` response. It writes screenshots plus `summary.json` and
`timeline.jsonl` under `artifacts/self-tests/`. See
`web/scripts/SELF-TEST.md` for flags and prerequisites.
