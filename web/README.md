# friday-web

Voice and transcript UI for friday. Three routes:

- `/` — `SessionsList`. REST against `GET /sessions`, `POST /sessions`.
- `/s/:id` — `VoiceRoom`. **Only page that imports `@pipecat-ai/voice-ui-kit`.**
- `/s/:id/transcript` — `SessionView`. REST + SSE for live transcript and state.

See [`../BackendIntegration.md`](../BackendIntegration.md) for the wire contract and
[`../jarvis.md`](../jarvis.md) for FE/BE separation rules.

## Dev

```sh
npm install
npm run dev      # http://localhost:5173
```

The backend runs separately at `http://localhost:8765` (default). Override with
`VITE_FRIDAY_BASE_URL`.

## Scripts

- `npm run dev` — Vite dev server
- `npm run build` — `tsc -b && vite build`
- `npm run typecheck` — `tsc -b`
- `npm run lint` — `eslint --max-warnings 0`
- `npm run lint:fix` — auto-fix
- `npm run format` / `format:check` — Prettier

## Hard rules (enforced by lint + structure)

- All HTTP goes through `@/lib/api.ts`. Direct `fetch` calls fail lint.
- `voice-ui-kit` and `pipecat` types only appear in `src/pages/VoiceRoom.tsx`.
- App data flows through REST/SSE — never RTVI custom messages.
- `max-lines: 700`. No god components.
