// E2E smoke: send a turn, collect TTS audio chunks, write MP3 to /tmp/e2e-tts.mp3.
import WebSocket from 'ws';
import { writeFileSync } from 'fs';

const ws = new WebSocket('ws://127.0.0.1:8787/ws');
let sessionId = null;
let deltaCount = 0;
let ttsChunks = 0;
let ttsBytes = 0;
const audioBuffers = [];
let agentDone = false;
let ttsEnded = false;

const TIMEOUT_MS = 90_000;

function maybeFinish() {
  if (agentDone && ttsEnded) {
    if (audioBuffers.length > 0) {
      const total = Buffer.concat(audioBuffers);
      writeFileSync('/tmp/e2e-tts.mp3', total);
      console.log(`[client] wrote /tmp/e2e-tts.mp3 (${total.length} bytes, ${ttsChunks} chunks)`);
    }
    ws.close();
    setTimeout(() => process.exit(ttsBytes > 0 ? 0 : 1), 200);
  }
}

ws.on('open', () => {
  console.log('[client] open');
  ws.send(JSON.stringify({ type: 'session.create', title: 'tts-smoke' }));
});

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString());
  if (msg.type === 'agent.text.delta') {
    deltaCount++;
    process.stdout.write(msg.text);
    return;
  }
  if (msg.type === 'tts.audio.chunk') {
    ttsChunks++;
    const bytes = Buffer.from(msg.audioBase64, 'base64');
    ttsBytes += bytes.length;
    audioBuffers.push(bytes);
    if (ttsChunks <= 3 || ttsChunks % 10 === 0) {
      console.log(`[client] tts chunk #${msg.sequence}: ${bytes.length}b (total ${ttsBytes})`);
    }
    return;
  }
  console.log('[client] <-', msg.type, JSON.stringify(msg).slice(0, 200));
  if (msg.type === 'session.created') {
    sessionId = msg.sessionId;
    ws.send(JSON.stringify({
      type: 'turn.send',
      sessionId,
      text: 'Say hello in exactly five words. No code, no formatting.',
      source: 'typed',
    }));
  } else if (msg.type === 'agent.status' && msg.status === 'done') {
    console.log('\n[client] agent done. deltas=', deltaCount);
    agentDone = true;
    maybeFinish();
  } else if (msg.type === 'tts.ended') {
    console.log('[client] tts ended. chunks=', ttsChunks, 'bytes=', ttsBytes);
    ttsEnded = true;
    maybeFinish();
  } else if (msg.type === 'error') {
    console.error('[client] ERROR', msg);
    process.exit(2);
  }
});

ws.on('close', () => console.log('[client] closed'));
ws.on('error', (e) => { console.error('[client] err', e); process.exit(3); });

setTimeout(() => {
  console.error('[client] timeout. agentDone=', agentDone, 'ttsEnded=', ttsEnded, 'bytes=', ttsBytes);
  process.exit(4);
}, TIMEOUT_MS);
