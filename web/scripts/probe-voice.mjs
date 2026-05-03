// Headless probe of the voice room.
//
// Launches Chromium with fake audio capture (no real mic needed), grants
// the microphone permission preemptively (no UI prompt), and disables
// Chrome's mDNS host-IP obfuscation so ICE checks don't pile up against
// .local hostnames the server can't quickly resolve.
//
// Captures: console messages, network requests touching the friday API,
// final RTCPeerConnection stats (bytesSent + iceConnectionState),
// signed-state from voice-ui-kit, screenshots on failure.
//
// Usage:
//   node scripts/probe-voice.mjs [SESSION_ID]
//
// If no SESSION_ID is given, the script creates a fresh opencode session
// via POST /sessions first.

import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const FRIDAY_BASE = process.env.FRIDAY_BASE_URL ?? 'http://localhost:8765';
const FE_BASE = process.env.FE_BASE_URL ?? 'http://localhost:5173';
const FAKE_AUDIO = process.env.FAKE_AUDIO_PATH ?? '/tmp/voice-test-input.wav';
const HOLD_MS = Number(process.env.HOLD_MS ?? 25_000);

if (!fs.existsSync(FAKE_AUDIO)) {
  console.error(`fake audio file missing: ${FAKE_AUDIO}`);
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
    '--disable-features=WebRtcHideLocalIpsWithMdns',
    '--allow-insecure-localhost',
    '--disable-web-security', // localhost only
  ],
});

const context = await browser.newContext({ permissions: ['microphone'] });
await context.grantPermissions(['microphone'], { origin: FE_BASE });
const page = await context.newPage();

// Capture console + network
const consoleLog = [];
page.on('console', (msg) => {
  consoleLog.push(`[${msg.type()}] ${msg.text()}`);
});
page.on('pageerror', (err) => consoleLog.push(`[pageerror] ${err.message}`));

const apiHits = [];
page.on('request', (req) => {
  const u = req.url();
  if (u.includes(FRIDAY_BASE) || u.includes('/api/offer') || u.includes('/sessions')) {
    apiHits.push(`-> ${req.method()} ${u}`);
  }
});
page.on('response', async (resp) => {
  const u = resp.url();
  if (u.includes(FRIDAY_BASE) || u.includes('/api/offer') || u.includes('/sessions')) {
    apiHits.push(`<- ${resp.status()} ${u}`);
  }
});

const url = `${FE_BASE}/s/${sessionId}`;
console.log(`[probe] navigating ${url}`);
await page.goto(url, { waitUntil: 'domcontentloaded' });

// Expose every RTCPeerConnection the page constructs so we can poll its
// stats from the test side. voice-ui-kit and pipecat-client-js construct
// the PC inside their own modules; we monkey-patch the constructor.
await page.addInitScript(() => {
  const originalRTCPC = window.RTCPeerConnection;
  if (!originalRTCPC) return;
  // @ts-ignore
  window.__pcs = [];
  // @ts-ignore
  window.RTCPeerConnection = function (...args) {
    const pc = new originalRTCPC(...args);
    // @ts-ignore
    window.__pcs.push(pc);
    return pc;
  };
  // copy prototype + statics
  // @ts-ignore
  window.RTCPeerConnection.prototype = originalRTCPC.prototype;
  for (const key of Object.keys(originalRTCPC)) {
    // @ts-ignore
    window.RTCPeerConnection[key] = originalRTCPC[key];
  }
});

// Reload so the init script applies (page.goto already happened, but
// the init script runs on the *next* navigation in this context).
await page.reload({ waitUntil: 'domcontentloaded' });

// Wait for the Connect button.
console.log('[probe] waiting for connect button');
const connectBtn = page.locator(
  'button:has-text("Connect"), button:has-text("connect"), [aria-label*="connect" i]',
);
try {
  await connectBtn.first().waitFor({ state: 'visible', timeout: 30_000 });
} catch (e) {
  await page.screenshot({ path: '/tmp/probe-no-connect.png', fullPage: true });
  console.error('[probe] connect button not found within 30s. screenshot at /tmp/probe-no-connect.png');
  console.error('--- console ---');
  console.error(consoleLog.join('\n'));
  process.exit(3);
}

console.log('[probe] clicking connect');
const t0 = Date.now();
await connectBtn.first().click();

// Hold the call for HOLD_MS, polling stats every 2s.
const samples = [];
const sampleEvery = 2000;
const samplesEnd = t0 + HOLD_MS;
while (Date.now() < samplesEnd) {
  await page.waitForTimeout(sampleEvery);
  const stats = await page
    .evaluate(async () => {
      const pcs = /** @type any */ (window).__pcs ?? [];
      const out = [];
      for (const pc of pcs) {
        const s = await pc.getStats();
        const o = { iceConnectionState: pc.iceConnectionState, signalingState: pc.signalingState, outbound: [], candidatePair: null };
        s.forEach((report) => {
          if (report.type === 'outbound-rtp' && report.kind === 'audio') {
            o.outbound.push({ bytesSent: report.bytesSent, packetsSent: report.packetsSent });
          }
          if (report.type === 'candidate-pair' && report.nominated) {
            o.candidatePair = {
              state: report.state,
              bytesSent: report.bytesSent,
              bytesReceived: report.bytesReceived,
            };
          }
        });
        out.push(o);
      }
      return out;
    })
    .catch((e) => ({ error: String(e) }));
  samples.push({ t: Date.now() - t0, stats });
  console.log(`[probe] +${Math.round((Date.now() - t0) / 1000)}s`, JSON.stringify(stats));
}

// Final screenshot for visual confirmation.
await page.screenshot({ path: '/tmp/probe-end.png', fullPage: true });

// Dump all artifacts.
const out = {
  sessionId,
  consoleLog,
  apiHits,
  samples,
};
fs.writeFileSync('/tmp/probe-voice-result.json', JSON.stringify(out, null, 2));
console.log('[probe] wrote /tmp/probe-voice-result.json and /tmp/probe-end.png');

await browser.close();
