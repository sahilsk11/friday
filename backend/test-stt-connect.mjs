// Verify the STT WebSocket actually opens to ElevenLabs (no real audio).
import WebSocket from 'ws';

const ws = new WebSocket('ws://127.0.0.1:8787/ws');
let sessionId = null;
let connected = false;
let firstError = null;

ws.on('open', () => {
  ws.send(JSON.stringify({ type: 'session.create', title: 'stt-connect' }));
});

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString());
  console.log('<-', msg.type, JSON.stringify(msg).slice(0, 180));
  if (msg.type === 'session.created') {
    sessionId = msg.sessionId;
    ws.send(JSON.stringify({
      type: 'audio.start',
      sessionId,
      sampleRate: 16000,
      encoding: 'pcm16',
    }));
  } else if (msg.type === 'session.state' && msg.state === 'listening') {
    connected = true;
  } else if (msg.type === 'error' && !firstError) {
    firstError = msg;
  }
});

setTimeout(() => {
  console.log('--- summary ---');
  console.log('connected (listening):', connected);
  console.log('first error:', firstError);
  ws.close();
  process.exit(connected && !firstError ? 0 : 1);
}, 6000);
