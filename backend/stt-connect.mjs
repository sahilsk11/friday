/**
 * Smoke test: opens the gateway WebSocket, sends audio.start for a fake
 * session, waits 2 seconds for any response (session.state, error, etc.),
 * then exits.
 *
 * Run with:  node stt-connect.mjs
 * Requires the gateway to be running: npm run dev
 */

import { WebSocket } from 'ws';

const WS_URL = process.env.GATEWAY_URL ?? 'ws://localhost:8787/ws';
const FAKE_SESSION_ID = 'smoke-test-session';

console.log(`Connecting to ${WS_URL} …`);

const ws = new WebSocket(WS_URL);
let receivedAny = false;

ws.on('open', () => {
  console.log('WS open — sending audio.start');
  ws.send(
    JSON.stringify({
      type: 'audio.start',
      sessionId: FAKE_SESSION_ID,
      sampleRate: 16000,
      encoding: 'pcm16',
    }),
  );
});

ws.on('message', (data) => {
  receivedAny = true;
  console.log('Received:', data.toString());
});

ws.on('error', (err) => {
  console.error('WS error:', err.message);
});

ws.on('close', (code, reason) => {
  console.log(`WS closed: ${code} ${reason.toString()}`);
});

// Wait 2 s then evaluate and exit
setTimeout(() => {
  if (!receivedAny) {
    console.warn('No messages received within 2s — gateway may not be running.');
  } else {
    console.log('Smoke test complete — received at least one message.');
  }
  ws.close();
  process.exit(0);
}, 2000);
