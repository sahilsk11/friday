// Real STT smoke: synthesize "hello world" via macOS `say`, encode as PCM16 16k,
// stream it through the gateway, and print partial+committed transcripts.
import WebSocket from 'ws';
import { execSync } from 'child_process';
import { readFileSync, statSync, unlinkSync } from 'fs';

const WAV_PATH = '/tmp/stt-test.wav';

console.log('[setup] synthesizing test phrase via `say`...');
try { unlinkSync(WAV_PATH); } catch {}
execSync(`say "Hello, this is a test of speech recognition." -o ${WAV_PATH} --data-format=LEI16@16000`);
const wavBuf = readFileSync(WAV_PATH);
console.log(`[setup] wav size: ${wavBuf.length} bytes`);

// Skip the 44-byte standard WAV header to get raw PCM16 LE.
let pcm = wavBuf.subarray(44);
console.log(`[setup] pcm size: ${pcm.length} bytes (~${(pcm.length / 32000).toFixed(2)}s of audio)`);

const ws = new WebSocket('ws://127.0.0.1:8787/ws');
let sessionId = null;
let partials = [];
let finals = [];
let errored = null;

ws.on('open', () => {
  ws.send(JSON.stringify({ type: 'session.create', title: 'stt-real' }));
});

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString());
  if (msg.type === 'session.created') {
    sessionId = msg.sessionId;
    console.log('[client] session:', sessionId);
    ws.send(JSON.stringify({
      type: 'audio.start',
      sessionId,
      sampleRate: 16000,
      encoding: 'pcm16',
    }));
    return;
  }
  if (msg.type === 'session.state' && msg.state === 'listening') {
    console.log('[client] listening — streaming PCM in 100ms chunks...');
    streamPcm();
    return;
  }
  if (msg.type === 'stt.partial') {
    partials.push(msg.text);
    console.log('[partial]', msg.text);
    return;
  }
  if (msg.type === 'stt.final') {
    finals.push(msg.text);
    console.log('[FINAL  ]', msg.text);
    return;
  }
  if (msg.type === 'error') {
    errored = msg;
    console.error('[error]', msg);
  }
});

function streamPcm() {
  // Send 100ms chunks: 16000 samples/s * 0.1 = 1600 samples = 3200 bytes
  const CHUNK = 3200;
  let seq = 0;
  let off = 0;
  const interval = setInterval(() => {
    if (off >= pcm.length) {
      clearInterval(interval);
      console.log('[client] done streaming, sending audio.stop');
      ws.send(JSON.stringify({ type: 'audio.stop', sessionId }));
      // Wait a bit for final transcript, then exit.
      setTimeout(finish, 5000);
      return;
    }
    const slice = pcm.subarray(off, off + CHUNK);
    off += CHUNK;
    const chunkBase64 = slice.toString('base64');
    ws.send(JSON.stringify({
      type: 'audio.chunk',
      sessionId,
      chunkBase64,
      sequence: seq++,
    }));
  }, 100);
}

function finish() {
  console.log('--- summary ---');
  console.log('partials:', partials.length, partials.length ? `last: "${partials[partials.length - 1]}"` : '');
  console.log('finals  :', finals.length, finals.length ? `joined: "${finals.join(' | ')}"` : '');
  console.log('errored :', errored);
  ws.close();
  process.exit(finals.length > 0 ? 0 : 1);
}

setTimeout(() => {
  console.error('[client] hard timeout');
  finish();
}, 30000);
