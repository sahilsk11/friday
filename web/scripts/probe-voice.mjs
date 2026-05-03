// Headless probe of the voice room, WebSocket transport edition.
//
// Launches Chromium with fake audio capture (no real mic needed) and grants
// the microphone permission preemptively. Captures: console messages,
// WebSocket open/close, time-to-connected, screenshots.
//
// Usage:
//   node scripts/probe-voice.mjs [SESSION_ID]
//
// If no SESSION_ID is given, the script creates a fresh opencode session
// via POST /sessions first.

import fs from 'node:fs';
import { chromium } from 'playwright';

const FRIDAY_BASE = process.env.FRIDAY_BASE_URL ?? 'http://localhost:8765';
const FE_BASE = process.env.FE_BASE_URL ?? 'http://localhost:5173';
const FAKE_AUDIO = process.env.FAKE_AUDIO_PATH ?? '/tmp/voice-test-input.wav';
const HOLD_MS = Number(process.env.HOLD_MS ?? 25_000);

if (!fs.existsSync(FAKE_AUDIO)) {
  console.error(`fake audio file missing: ${FAKE_AUDIO}`);
  console.error('regenerate with: say "hey friday what does this code do" -o /tmp/voice-test-input.wav --data-format=LEI16@16000');
  process.exit(2);
}

async function ensureSession(arg) {
  if (arg) return arg;
  const r = await fetch(`${FRIDAY_BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'probe-voice' }),
  });
  if (!r.ok) throw new Error(`POST /sessions failed: ${r.status}`);
  const row = await r.json();
  return row.id;
}

const sessionId = await ensureSession(process.argv[2]);
console.log(`[probe] session=${sessionId}`);

const browser = await chromium.launch({
  headless: true,
  args: [
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${FAKE_AUDIO}`,
    '--autoplay-policy=no-user-gesture-required',
  ],
});

const ctx = await browser.newContext({ permissions: ['microphone'] });
await ctx.grantPermissions(['microphone'], { origin: FE_BASE });
const page = await ctx.newPage();

const consoleLog = [];
page.on('console', (m) => consoleLog.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', (e) => consoleLog.push(`[pageerror] ${e.message}`));

const wsEvents = [];
const wsCounters = new Map();
page.on('websocket', (ws) => {
  wsEvents.push(`OPENED ${ws.url()}`);
  wsCounters.set(ws.url(), { sent: 0, received: 0, sentBytes: 0, recvBytes: 0 });
  ws.on('framesent', (data) => {
    const c = wsCounters.get(ws.url());
    if (!c) return;
    c.sent++;
    c.sentBytes += data.payload.length;
  });
  ws.on('framereceived', (data) => {
    const c = wsCounters.get(ws.url());
    if (!c) return;
    c.received++;
    c.recvBytes += data.payload.length;
  });
  ws.on('close', () => wsEvents.push(`CLOSED ${ws.url()}`));
  ws.on('socketerror', (e) => wsEvents.push(`ERROR ${ws.url()} ${String(e)}`));
});

const url = `${FE_BASE}/s/${sessionId}`;
console.log(`[probe] navigating ${url}`);
await page.goto(url, { waitUntil: 'domcontentloaded' });

await page.locator('button:has-text("Connect")').first().waitFor({ state: 'visible', timeout: 20_000 });
console.log('[probe] connect button visible');

const t0 = Date.now();
console.log('[probe] clicking connect');
await page.locator('button:has-text("Connect")').first().click();

// Poll button text + WS event count.
const samples = [];
let connectedAt = null;
const sampleEvery = 1000;
const samplesEnd = t0 + HOLD_MS;
while (Date.now() < samplesEnd) {
  await page.waitForTimeout(sampleEvery);
  const status = await page.evaluate(() => {
    const pill = Array.from(document.querySelectorAll('div')).find(
      (el) =>
        el.classList.contains('items-center') &&
        el.classList.contains('text-xs') &&
        el.querySelector('span.rounded-full'),
    );
    return pill?.textContent?.trim() ?? null;
  });
  // Live mic level — proves Web Audio is actually capturing samples.
  const micPower = await page.evaluate(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);
      // Wait one tick so the analyser fills.
      await new Promise((r) => setTimeout(r, 80));
      analyser.getByteFrequencyData(data);
      const sum = data.reduce((a, b) => a + b, 0);
      stream.getTracks().forEach((t) => t.stop());
      void ctx.close();
      return sum;
    } catch (e) {
      return -1;
    }
  });
  const elapsed = Math.round((Date.now() - t0) / 100) / 10;
  samples.push({ t: elapsed, status, micPower });
  if (status && /^(ready|connected)$/.test(status) && connectedAt === null) {
    connectedAt = elapsed;
    console.log(`[probe] CONNECTED at +${elapsed}s (state=${status})`);
  }
}

await page.screenshot({ path: '/tmp/probe-end.png', fullPage: true });

const result = { sessionId, connectedAt, wsEvents, samples, consoleLog };
fs.writeFileSync('/tmp/probe-voice-result.json', JSON.stringify(result, null, 2));

console.log('--- ws events ---');
wsEvents.forEach((e) => console.log(e));
console.log('--- ws counters ---');
for (const [url, c] of wsCounters) {
  console.log(`${url}\n  sent=${c.sent} (${c.sentBytes}b) received=${c.received} (${c.recvBytes}b)`);
}
console.log('--- summary ---');
console.log(`time-to-connected: ${connectedAt === null ? 'NEVER' : connectedAt + 's'}`);
console.log('saved /tmp/probe-voice-result.json + /tmp/probe-end.png');

await browser.close();
