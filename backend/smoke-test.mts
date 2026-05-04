// Smoke test: session.create + turn.send → expect streaming events.
// Run with: node --env-file=.env --import tsx/esm smoke-test.mts
import WebSocket from 'ws';

const WS_URL = 'ws://localhost:8787/ws';
const TIMEOUT_MS = 30000;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function run(): Promise<void> {
  console.warn('Connecting to', WS_URL);
  const ws = new WebSocket(WS_URL);

  const received: unknown[] = [];
  let sessionId: string | null = null;
  let done = false;

  ws.on('open', () => {
    console.warn('[OPEN] Connected');
    ws.send(JSON.stringify({ type: 'session.create' }));
  });

  ws.on('message', (data: WebSocket.RawData) => {
    const msg = JSON.parse(data.toString()) as { type: string; [key: string]: unknown };
    received.push(msg);
    console.warn('[MSG]', JSON.stringify(msg).slice(0, 200));

    if (msg['type'] === 'session.created') {
      sessionId = msg['sessionId'] as string;
      console.warn('[INFO] Got sessionId:', sessionId);
      // Send a turn immediately
      ws.send(
        JSON.stringify({
          type: 'turn.send',
          sessionId,
          text: 'say hello in one word',
          source: 'typed',
        }),
      );
    }

    if (msg['type'] === 'agent.text.final' || msg['type'] === 'agent.status' && msg['status'] === 'done') {
      if (!done) {
        done = true;
        console.warn('\n[DONE] Smoke test PASSED. Events received:');
        for (const r of received) {
          console.warn(' -', (r as { type: string }).type);
        }
        ws.close();
      }
    }
  });

  ws.on('error', (err) => {
    console.error('[ERROR]', err);
  });

  ws.on('close', () => {
    console.warn('[CLOSE] Connection closed');
  });

  // Timeout guard
  await delay(TIMEOUT_MS);
  if (!done) {
    console.error('[TIMEOUT] Did not receive agent.text.final within', TIMEOUT_MS, 'ms');
    console.warn('Events received so far:');
    for (const r of received) {
      console.warn(' -', JSON.stringify(r).slice(0, 300));
    }
    ws.close();
    process.exit(1);
  }
}

await run();
