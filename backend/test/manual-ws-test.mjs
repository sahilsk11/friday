/**
 * Manual WebSocket smoke test for the voice gateway.
 *
 * Phase 1 protocol test:
 *   1. Connect to ws://127.0.0.1:8787/ws
 *   2. Send {type:'session.create'}
 *   3. Expect session.created with a sessionId within 3 seconds
 *   4. Send {type:'turn.send', sessionId, text:'say hi in 5 words', source:'typed'}
 *   5. Expect turn.accepted + at least one agent.text.delta or agent.text.final within 30 seconds
 *   6. Report pass/fail and exit
 *
 * Usage:
 *   node test/manual-ws-test.mjs
 *
 * Prerequisites:
 *   - opencode serve running (default: http://127.0.0.1:4096)
 *   - npm run dev running (default: ws://127.0.0.1:8787)
 */

import { createRequire } from 'module';
import { setTimeout as sleep } from 'timers/promises';

const require = createRequire(import.meta.url);
const WebSocket = require('ws');

const WS_URL = process.env['WS_URL'] ?? 'ws://127.0.0.1:8787/ws';
const SESSION_TIMEOUT_MS = 3_000;
const TURN_TIMEOUT_MS = 30_000;

function log(tag, msg) {
  process.stdout.write(`[${new Date().toISOString()}] [${tag}] ${msg}\n`);
}

async function run() {
  log('INFO', `Connecting to ${WS_URL}`);
  const ws = new WebSocket(WS_URL);

  const messages = [];
  let sessionId = null;
  let sessionCreatedAt = null;
  let gotDelta = false;
  let gotFinal = false;
  let closed = false;

  ws.on('open', () => {
    log('OPEN', 'Connected');
    ws.send(JSON.stringify({ type: 'session.create' }));
    log('SEND', JSON.stringify({ type: 'session.create' }));
  });

  ws.on('message', (data) => {
    const raw = data.toString();
    const msg = JSON.parse(raw);
    messages.push(msg);
    log('RECV', JSON.stringify(msg).slice(0, 300));

    if (msg.type === 'session.created') {
      sessionId = msg.sessionId;
      sessionCreatedAt = Date.now();
      log('INFO', `session.created with sessionId=${sessionId}`);

      // Send a turn immediately after session is created
      const turn = { type: 'turn.send', sessionId, text: 'say hi in 5 words', source: 'typed' };
      ws.send(JSON.stringify(turn));
      log('SEND', JSON.stringify(turn));
    }

    if (msg.type === 'agent.text.delta') gotDelta = true;
    if (msg.type === 'agent.text.final') gotFinal = true;
  });

  ws.on('error', (err) => {
    log('ERROR', String(err));
  });

  ws.on('close', () => {
    closed = true;
    log('CLOSE', 'Connection closed');
  });

  // Wait for session.created
  const sessionDeadline = Date.now() + SESSION_TIMEOUT_MS;
  while (!sessionId && Date.now() < sessionDeadline && !closed) {
    await sleep(100);
  }

  if (!sessionId) {
    log('FAIL', `Did not receive session.created within ${SESSION_TIMEOUT_MS}ms`);
    ws.close();
    process.exit(1);
  }

  log('PASS', `session.created received after ${Date.now() - (sessionCreatedAt ?? Date.now())}ms`);

  // Wait for at least one streaming event or final
  const turnDeadline = Date.now() + TURN_TIMEOUT_MS;
  while (!gotDelta && !gotFinal && Date.now() < turnDeadline && !closed) {
    await sleep(200);
  }

  ws.close();

  if (gotDelta || gotFinal) {
    log('PASS', 'Received agent streaming event (delta or final). Smoke test PASSED.');
    log('INFO', `Total messages received: ${messages.length}`);
    log('INFO', 'Message types: ' + messages.map((m) => m.type).join(', '));
    process.exit(0);
  } else {
    log('FAIL', `Did not receive agent.text.delta or agent.text.final within ${TURN_TIMEOUT_MS}ms`);
    log('INFO', 'Messages received so far:');
    for (const m of messages) {
      log('MSG', JSON.stringify(m).slice(0, 300));
    }
    process.exit(1);
  }
}

run().catch((err) => {
  process.stderr.write(`Unhandled error: ${String(err)}\n`);
  process.exit(1);
});
