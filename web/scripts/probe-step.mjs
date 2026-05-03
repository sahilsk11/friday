// Step-by-step probe to find where the connect flow stalls.
import { chromium } from 'playwright';
import fs from 'node:fs';

const FE = 'http://localhost:5173';
const FAKE_AUDIO = '/tmp/voice-test-input.wav';
if (!fs.existsSync(FAKE_AUDIO)) throw new Error('missing fake audio');

const browser = await chromium.launch({
  headless: true,
  args: [
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${FAKE_AUDIO}`,
    '--autoplay-policy=no-user-gesture-required',
    '--disable-features=WebRtcHideLocalIpsWithMdns',
  ],
});

const ctx = await browser.newContext({ permissions: ['microphone'] });
await ctx.grantPermissions(['microphone'], { origin: FE });

// Inject RTCPC tracker BEFORE first navigation.
await ctx.addInitScript(() => {
  // @ts-ignore
  window.__pcs = [];
  const O = window.RTCPeerConnection;
  // @ts-ignore
  window.RTCPeerConnection = new Proxy(O, {
    construct(target, args) {
      const pc = new target(...args);
      // @ts-ignore
      window.__pcs.push(pc);
      console.log('[probe] RTCPeerConnection constructed; total=', window.__pcs.length);
      return pc;
    },
  });
});

const page = await ctx.newPage();
const log = [];
page.on('console', (m) => log.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', (e) => log.push(`[pageerror] ${e.message}`));

// Create a session via REST.
const r = await fetch('http://localhost:8765/sessions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ title: 'probe-step' }),
});
const sess = await r.json();
console.log('session', sess.id);

await page.goto(`${FE}/s/${sess.id}`, { waitUntil: 'domcontentloaded' });

// Wait for ConnectButton to appear, then poll for client readiness.
await page.locator('button:has-text("Connect")').first().waitFor({ state: 'visible', timeout: 20_000 });
console.log('connect button visible');

// Test 1: can we run getUserMedia ourselves?
const gum = await page.evaluate(async () => {
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    const tracks = s.getAudioTracks().map((t) => ({
      label: t.label,
      enabled: t.enabled,
      muted: t.muted,
      readyState: t.readyState,
      settings: t.getSettings(),
    }));
    s.getTracks().forEach((t) => t.stop());
    return { ok: true, tracks };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});
console.log('getUserMedia:', JSON.stringify(gum, null, 2));

const devices = await page.evaluate(async () => {
  const ds = await navigator.mediaDevices.enumerateDevices();
  return ds.map((d) => ({ kind: d.kind, label: d.label }));
});
console.log('devices:', JSON.stringify(devices, null, 2));

// Check the page button enabled state.
const btnState = await page.evaluate(() => {
  const btn = Array.from(document.querySelectorAll('button')).find((b) =>
    /connect/i.test(b.textContent ?? ''),
  );
  if (!btn) return { found: false };
  return {
    found: true,
    text: btn.textContent,
    disabled: btn.disabled,
    ariaDisabled: btn.getAttribute('aria-disabled'),
    boundingClient: btn.getBoundingClientRect(),
  };
});
console.log('button state:', JSON.stringify(btnState, null, 2));

console.log('clicking connect...');
const t0 = Date.now();
await page.locator('button:has-text("Connect")').first().click();

// Sample for 20s.
for (let i = 0; i < 10; i++) {
  await page.waitForTimeout(2000);
  const state = await page.evaluate(() => {
    const pcs = /** @type any */ (window).__pcs ?? [];
    const out = pcs.map((pc) => ({
      iceConnectionState: pc.iceConnectionState,
      signalingState: pc.signalingState,
      connectionState: pc.connectionState,
    }));
    const btn = Array.from(document.querySelectorAll('button')).find((b) =>
      /connect|disconnect/i.test(b.textContent ?? ''),
    );
    return { pcs: out, btnText: btn?.textContent };
  });
  console.log(`+${(Date.now() - t0) / 1000}s`, JSON.stringify(state));
}

await page.screenshot({ path: '/tmp/probe-step.png', fullPage: true });
fs.writeFileSync('/tmp/probe-step-log.txt', log.join('\n'));
console.log('--- console snippets (last 40) ---');
log.slice(-40).forEach((l) => console.log(l));

await browser.close();
