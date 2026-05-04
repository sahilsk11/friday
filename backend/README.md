# Voice Gateway — Phase 1

Local Node.js backend that bridges the browser WebSocket protocol to OpenCode and ElevenLabs (Phase 2).

## Quickstart

### 1. Start OpenCode server

```bash
opencode serve --port 4096
```

OpenCode will listen on `http://127.0.0.1:4096` by default.

### 2. Install dependencies

```bash
npm install
```

### 3. Configure environment

Copy or create `.env` in the `backend/` directory:

```env
PORT=8787
OPENCODE_BASE_URL=http://127.0.0.1:4096
ELEVENLABS_API_KEY=your_key_here
LOG_LEVEL=info
```

### 4. Start the gateway

```bash
npm run dev
```

The gateway listens at `ws://localhost:8787/ws`.

### 5. Smoke test

With both `opencode serve` and `npm run dev` running:

```bash
node test/manual-ws-test.mjs
```

Expected output: `session.created` followed by `turn.accepted` and at least one `agent.text.delta` or `agent.text.final`.

## Scripts

| Command | Description |
|---|---|
| `npm run dev` | Start with tsx watch (hot reload) |
| `npm run build` | Compile TypeScript to `dist/` |
| `npm start` | Run compiled output |
| `npm run typecheck` | Type-check without emitting |
| `npm run lint` | Run ESLint (0 warnings allowed) |
| `npm run lint:fix` | Auto-fix lint issues |
| `npm run format` | Format with Prettier |
