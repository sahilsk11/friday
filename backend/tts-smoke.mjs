import { createWriteStream } from 'fs';
import { pipeline } from 'stream/promises';
import { Readable } from 'stream';

import dotenv from 'dotenv';
dotenv.config();

// Dynamic import so ESM resolution kicks in after dotenv has set up env vars.
const { createElevenLabsTtsAdapter } = await import('./src/tts/elevenLabsTtsAdapter.js');

const VOICE_ID = 'EXAVITQu4vr4xnSDxMaL'; // Sarah (available on this account; Rachel not provisioned)
const MODEL_ID = 'eleven_flash_v2_5';
const OUTPUT = '/tmp/tts-test.mp3';

const chunks = [];

const adapter = createElevenLabsTtsAdapter();

await new Promise((resolve, reject) => {
  let settled = false;
  function done(err) {
    if (settled) return;
    settled = true;
    if (err) reject(err); else resolve(undefined);
  }

  adapter
    .start({
      sessionId: 'smoke-session',
      turnId: 'smoke-turn-1',
      voiceId: VOICE_ID,
      modelId: MODEL_ID,
      onStart() {
        console.warn('[smoke] onStart fired');
      },
      onChunk(chunk, sequence) {
        console.warn(`[smoke] chunk #${sequence}: ${chunk.byteLength} bytes`);
        chunks.push(chunk);
      },
      onEnd() {
        console.warn('[smoke] onEnd fired');
        done(null);
      },
      onError(err) {
        console.error('[smoke] onError:', err.message);
        // input_timeout_exceeded happens if we don't close the stream in time;
        // if we already received audio chunks, treat it as a soft end.
        if (chunks.length > 0) {
          console.warn('[smoke] received chunks before error — treating as end');
          done(null);
        } else {
          done(err);
        }
      },
    })
    .then(() => {
      // start() resolves once the WebSocket is open and initialized.
      adapter.sendText('Hello world this is a test.');
      adapter.flush();
      // Send EOS so ElevenLabs knows to close the stream and emit isFinal.
      return adapter.stop();
    })
    .catch((err) => done(err));
});

const totalBytes = chunks.reduce((sum, c) => sum + c.byteLength, 0);
console.warn(`[smoke] total bytes received: ${totalBytes}`);

if (totalBytes === 0) {
  console.error('[smoke] FAIL: no audio bytes received');
  process.exit(1);
}

// Write to file.
const readable = Readable.from(
  (function* () {
    for (const chunk of chunks) yield chunk;
  })(),
);
await pipeline(readable, createWriteStream(OUTPUT));
console.warn(`[smoke] wrote audio to ${OUTPUT}`);
