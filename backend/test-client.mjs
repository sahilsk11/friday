// Manual WS smoke test for the voice gateway.
import WebSocket from 'ws';

const ws = new WebSocket('ws://127.0.0.1:8787/ws');
let sessionId = null;
let gotFinal = false;
let deltaCount = 0;

const TIMEOUT_MS = 60_000;
const startedAt = Date.now();

ws.on('open', () => {
  console.log('[client] open');
  ws.send(JSON.stringify({ type: 'session.create', title: 'smoke' }));
});

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString());
  if (msg.type === 'agent.text.delta') {
    deltaCount++;
    process.stdout.write(msg.text);
    return;
  }
  console.log('[client] <-', msg.type, JSON.stringify(msg).slice(0, 200));
  if (msg.type === 'session.created') {
    sessionId = msg.sessionId;
    ws.send(JSON.stringify({
      type: 'turn.send',
      sessionId,
      text: 'Say hello in exactly 5 words.',
      source: 'typed',
    }));
  } else if (msg.type === 'agent.text.final') {
    gotFinal = true;
  } else if (msg.type === 'agent.status' && msg.status === 'done') {
    console.log('\n[client] done. deltas=', deltaCount, 'final?=', gotFinal);
    ws.close();
    setTimeout(() => process.exit(gotFinal ? 0 : 1), 200);
  } else if (msg.type === 'error') {
    console.error('[client] ERROR', msg);
    process.exit(2);
  }
});

ws.on('close', () => console.log('[client] closed'));
ws.on('error', (e) => { console.error('[client] err', e); process.exit(3); });

setTimeout(() => {
  console.error('[client] timeout. deltas=', deltaCount, 'final?=', gotFinal);
  process.exit(4);
}, TIMEOUT_MS);
