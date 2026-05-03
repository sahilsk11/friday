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
  console.error(
    'regenerate with: say "hey friday what does this code do" -o /tmp/voice-test-input.wav --data-format=LEI16@16000',
  );
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

// voice-ui-kit's <ConnectButton> renders "Connect" by default; case-insensitive match
// keeps this resilient to label tweaks.
const connectBtn = page.locator('button', { hasText: /^connect$/i }).first();
await connectBtn.waitFor({ state: 'visible', timeout: 20_000 });
console.log('[probe] connect button visible');

const t0 = Date.now();
console.log('[probe] clicking connect');
await connectBtn.click();

// Wait a few seconds for audio to start streaming, then tap Send to
// force ElevenLabs to commit. Without this tap, MANUAL commit mode
// holds the transcript open forever (see TRANSPORT.md).
const SEND_AFTER_MS = Number(process.env.SEND_AFTER_MS ?? 6000);
const sendBtn = page.locator('button', { hasText: /^send turn/i }).first();
setTimeout(() => {
  sendBtn
    .click()
    .then(() => console.log('[probe] tapped Send turn'))
    .catch((err) => console.log(`[probe] Send tap failed: ${err.message}`));
}, SEND_AFTER_MS);

// Poll status pill + WS event count + activity-feed entries.
const samples = [];
let connectedAt = null;
const sampleEvery = 1000;
const samplesEnd = t0 + HOLD_MS;
while (Date.now() < samplesEnd) {
  await page.waitForTimeout(sampleEvery);
  const status = await page.evaluate(() => {
    // <ClientStatus> renders the transport state as plain text inside its
    // wrapper. We walk the DOM looking for the known state strings.
    const known = [
      'ready',
      'connected',
      'connecting',
      'authenticating',
      'initializing',
      'disconnected',
      'error',
      'authenticated',
    ];
    const candidates = Array.from(document.querySelectorAll('div, span'));
    for (const el of candidates) {
      const txt = el.textContent?.trim().toLowerCase() ?? '';
      if (known.includes(txt)) return txt;
    }
    return null;
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
  // Read the activity feed: count entries and capture any tool/assistant
  // text. Proves RTVI server messages from OpencodeProcessor reached the UI.
  const feed = await page.evaluate(() => {
    const items = Array.from(document.querySelectorAll('ol > li'));
    return items.map((li) => li.textContent?.trim() ?? '');
  });
  const elapsed = Math.round((Date.now() - t0) / 100) / 10;
  samples.push({ t: elapsed, status, micPower, feedCount: feed.length });
  if (status && /^(ready|connected)$/.test(status) && connectedAt === null) {
    connectedAt = elapsed;
    console.log(`[probe] CONNECTED at +${elapsed}s (state=${status})`);
  }
}

// Capture the final feed for the result file.
const finalFeed = await page.evaluate(() => {
  const items = Array.from(document.querySelectorAll('ol > li'));
  return items.map((li) => li.textContent?.trim() ?? '');
});

await page.screenshot({ path: '/tmp/probe-end.png', fullPage: true });

const result = { sessionId, connectedAt, wsEvents, samples, finalFeed, consoleLog };
fs.writeFileSync('/tmp/probe-voice-result.json', JSON.stringify(result, null, 2));

console.log('--- ws events ---');
wsEvents.forEach((e) => console.log(e));
console.log('--- ws counters ---');
for (const [url, c] of wsCounters) {
  console.log(
    `${url}\n  sent=${c.sent} (${c.sentBytes}b) received=${c.received} (${c.recvBytes}b)`,
  );
}
console.log('--- final activity feed ---');
finalFeed.forEach((line, i) => console.log(`  ${i}. ${line}`));
console.log('--- summary ---');
console.log(`time-to-connected: ${connectedAt === null ? 'NEVER' : connectedAt + 's'}`);
console.log(`feed entries: ${finalFeed.length}`);
console.log('saved /tmp/probe-voice-result.json + /tmp/probe-end.png');

await browser.close();
