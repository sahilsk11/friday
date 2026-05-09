// Step-by-step probe to find where the connect flow stalls.
import fs from 'node:fs';
import { createSessionThroughUi, FE_BASE, launchFakeMicBrowser } from './probe-lib.mjs';

const existingSessionId = process.argv[2];
const { browser, ctx } = await launchFakeMicBrowser();

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

const url = existingSessionId ? `${FE_BASE}/s/${existingSessionId}` : FE_BASE;
await page.goto(url, { waitUntil: 'domcontentloaded' });
if (!existingSessionId) {
  await createSessionThroughUi(page, 'probe-step');
}
console.log('route', page.url());

await page.waitForLoadState('networkidle');
console.log('voice room loaded');

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

const t0 = Date.now();

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
      /start|send|interrupt/i.test(b.textContent ?? ''),
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
