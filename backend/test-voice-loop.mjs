// Full voice loop: speak via macOS `say` → STT → OpenCode → TTS audio out.
import WebSocket from 'ws';
import { execSync } from 'child_process';
import { readFileSync, writeFileSync, unlinkSync } from 'fs';

const WAV_PATH = '/tmp/voice-loop-in.wav';
const OUT_MP3 = '/tmp/voice-loop-out.mp3';

console.log('[setup] synthesizing input...');
try { unlinkSync(WAV_PATH); } catch {}
execSync(`say "Hello, please reply with one short sentence." -o ${WAV_PATH} --data-format=LEI16@16000`);
const pcm = readFileSync(WAV_PATH).subarray(44);
console.log(`[setup] pcm: ${pcm.length} bytes (~${(pcm.length / 32000).toFixed(2)}s)`);

const ws = new WebSocket('ws://127.0.0.1:8787/ws');
let sessionId = null;
let sttFinal = null;
let agentText = '';
let ttsChunks = 0;
const audioBuffers = [];
let agentDone = false;
let ttsEnded = false;

ws.on('open', () => ws.send(JSON.stringify({ type: 'session.create', title: 'voice-loop' })));

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString());
  if (msg.type === 'session.created') {
    sessionId = msg.sessionId;
    console.log('[loop] session:', sessionId);
    ws.send(JSON.stringify({ type: 'audio.start', sessionId, sampleRate: 16000, encoding: 'pcm16' }));
  } else if (msg.type === 'session.state' && msg.state === 'listening') {
    streamPcm();
  } else if (msg.type === 'stt.partial') {
    process.stdout.write(`\r[partial] ${msg.text}     `);
  } else if (msg.type === 'stt.final') {
    sttFinal = msg.text;
    console.log(`\n[STT final] ${msg.text}`);
  } else if (msg.type === 'agent.text.delta') {
    agentText += msg.text;
    process.stdout.write(msg.text);
  } else if (msg.type === 'agent.status' && msg.status === 'done') {
    console.log(`\n[agent done] full text: "${agentText}"`);
    agentDone = true;
    maybeFinish();
  } else if (msg.type === 'tts.audio.chunk') {
    ttsChunks++;
    audioBuffers.push(Buffer.from(msg.audioBase64, 'base64'));
  } else if (msg.type === 'tts.ended') {
    const total = Buffer.concat(audioBuffers);
    writeFileSync(OUT_MP3, total);
    console.log(`[TTS] wrote ${OUT_MP3} (${total.length} bytes, ${ttsChunks} chunks)`);
    ttsEnded = true;
    maybeFinish();
  } else if (msg.type === 'error') {
    console.error('[error]', msg);
  }
});

function maybeFinish() {
  if (agentDone && ttsEnded) {
    console.log('\n=== VOICE LOOP COMPLETE ===');
    console.log(`STT in : "${sttFinal}"`);
    console.log(`AGENT  : "${agentText}"`);
    console.log(`TTS out: ${OUT_MP3}`);
    ws.close();
    process.exit(0);
  }
}

function streamPcm() {
  const CHUNK = 3200;
  let seq = 0, off = 0;
  const interval = setInterval(() => {
    if (off >= pcm.length) {
      clearInterval(interval);
      ws.send(JSON.stringify({ type: 'audio.stop', sessionId }));
      return;
    }
    const slice = pcm.subarray(off, off + CHUNK);
    off += CHUNK;
    ws.send(JSON.stringify({
      type: 'audio.chunk',
      sessionId,
      chunkBase64: slice.toString('base64'),
      sequence: seq++,
    }));
  }, 100);
}

setTimeout(() => { console.error('\n[hard timeout]'); ws.close(); process.exit(1); }, 90000);
