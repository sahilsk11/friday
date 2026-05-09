import { execFile } from 'node:child_process';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const execFileP = promisify(execFile);

const { values: args } = parseArgs({
  options: {
    task: { type: 'string' },
    harness: { type: 'string' },
    directory: { type: 'string' },
    title: { type: 'string' },
    model: { type: 'string' },
    output: { type: 'string' },
    'fe-base-url': { type: 'string' },
    'existing-session': { type: 'string' },
    'leading-silence-seconds': { type: 'string' },
    'wait-after-start-ms': { type: 'string' },
    'wait-after-send-ms': { type: 'string' },
    'cdp-port': { type: 'string' },
    headless: { type: 'boolean' },
  },
  strict: true,
});

const FE_BASE = args['fe-base-url'] ?? process.env.FE_BASE_URL ?? 'http://localhost:5173';
const DIRECTORY =
  args.directory ??
  process.env.PROBE_DIRECTORY ??
  path.resolve(path.dirname(new URL(import.meta.url).pathname), '../..');
const HARNESS = args.harness ?? process.env.PROBE_HARNESS ?? 'opencode';
const MODEL = args.model ?? process.env.PROBE_MODEL ?? 'opencode/minimax-m2.5-free';
const TITLE =
  args.title ??
  process.env.PROBE_TITLE ??
  `voice-trace-${new Date().toISOString().replace(/[:.]/g, '')}`;
const TASK =
  args.task ??
  process.env.PROBE_TASK ??
  'Friday, can you hear me? Please reply with exactly one short sentence.';
const ARTIFACTS_ROOT =
  args.output ?? process.env.PROBE_OUTPUT ?? path.resolve(DIRECTORY, 'artifacts/friday-conversations');
const HEADLESS = args.headless || process.env.HEADLESS === '1';
const CDP_PORT = Number(args['cdp-port'] ?? process.env.CDP_PORT ?? 9234);
const LEADING_SILENCE_SECONDS = Number(
  args['leading-silence-seconds'] ?? process.env.LEADING_SILENCE_SECONDS ?? 8,
);
const WAIT_AFTER_START_MS = Number(
  args['wait-after-start-ms'] ?? process.env.WAIT_AFTER_START_MS ?? 15_000,
);
const WAIT_AFTER_SEND_MS = Number(
  args['wait-after-send-ms'] ?? process.env.WAIT_AFTER_SEND_MS ?? 45_000,
);
const EXISTING_SESSION = args['existing-session'] ?? null;

const runId = new Date().toISOString().replace(/[:.]/g, '');
const artifactsDir = path.join(ARTIFACTS_ROOT, `${runId}-${slug(TITLE)}`);
const screenshotsDir = path.join(artifactsDir, 'screenshots');
await fsp.mkdir(screenshotsDir, { recursive: true });

const tracePath = path.join(artifactsDir, 'timeline.jsonl');
const traceStream = fs.createWriteStream(tracePath, { flags: 'a' });
const startedAtNs = process.hrtime.bigint();
const startedAtWall = Date.now();

function elapsedMs() {
  return Number(process.hrtime.bigint() - startedAtNs) / 1_000_000;
}

function trace(event, data = {}) {
  const row = {
    tMs: Math.round(elapsedMs()),
    wallTime: new Date(startedAtWall + elapsedMs()).toISOString(),
    event,
    ...data,
  };
  traceStream.write(`${JSON.stringify(row)}\n`);
  return row;
}

function slug(value) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 48);
}

function modelParts(value) {
  const idx = value.indexOf('/');
  if (idx === -1) return null;
  return { providerID: value.slice(0, idx), modelID: value.slice(idx + 1) };
}

async function screenshot(page, name) {
  const filename = `${String(traceIndex++).padStart(2, '0')}-${name}.png`;
  const target = path.join(screenshotsDir, filename);
  await page.screenshot({ path: target, fullPage: true });
  trace('screenshot', { name, path: target });
  return target;
}

async function makeInputAudio() {
  const aiff = path.join(artifactsDir, 'input.aiff');
  const speechWav = path.join(artifactsDir, 'speech.wav');
  const inputWav = path.join(artifactsDir, 'input.wav');
  trace('audio-generate-start', { task: TASK, leadingSilenceSeconds: LEADING_SILENCE_SECONDS });
  await execFileP('/usr/bin/say', ['-v', 'Samantha', '-o', aiff, TASK]);
  await execFileP('/opt/homebrew/bin/ffmpeg', [
    '-y',
    '-hide_banner',
    '-loglevel',
    'error',
    '-i',
    aiff,
    '-ac',
    '2',
    '-ar',
    '44100',
    '-sample_fmt',
    's16',
    speechWav,
  ]);
  await execFileP('/opt/homebrew/bin/ffmpeg', [
    '-y',
    '-hide_banner',
    '-loglevel',
    'error',
    '-f',
    'lavfi',
    '-i',
    `anullsrc=r=44100:cl=stereo:d=${LEADING_SILENCE_SECONDS}`,
    '-i',
    speechWav,
    '-filter_complex',
    '[0:a][1:a]concat=n=2:v=0:a=1',
    '-ac',
    '2',
    '-ar',
    '44100',
    '-sample_fmt',
    's16',
    inputWav,
  ]);
  trace('audio-generate-complete', { aiff, speechWav, inputWav });
  return inputWav;
}

async function readUi(page) {
  return page.evaluate(() => {
    const body = document.body.innerText;
    const feed = Array.from(document.querySelectorAll('ol > li')).map(
      (li) => li.textContent?.trim() ?? '',
    );
    const buttons = Array.from(document.querySelectorAll('button')).map(
      (button) => button.textContent?.trim() ?? '',
    );
    const status = {
      client: /Client\s+([A-Z-]+)/i.exec(body)?.[1] ?? null,
      agent: /Agent\s+([A-Z-]+)/i.exec(body)?.[1] ?? null,
      mic: /mic:\s+(on|off)/i.exec(body)?.[1] ?? null,
      speaker: /speaker:\s+(on|off)/i.exec(body)?.[1] ?? null,
    };
    return {
      url: window.location.href,
      status,
      buttons,
      feed,
      bodyTail: body.slice(-1200),
    };
  });
}

function summarizeUi(ui) {
  return {
    url: ui.url,
    status: ui.status,
    buttons: ui.buttons,
    feedCount: ui.feed.length,
    feedTail: ui.feed.slice(-4),
  };
}

async function sampleLoop(page, label, durationMs, intervalMs = 1000) {
  const deadline = Date.now() + durationMs;
  let previous = null;
  while (Date.now() < deadline) {
    await page.waitForTimeout(intervalMs);
    const ui = await readUi(page);
    const current = JSON.stringify({
      status: ui.status,
      buttons: ui.buttons,
      feed: ui.feed,
    });
    if (current !== previous) {
      trace('ui-change', { label, ...summarizeUi(ui) });
      previous = current;
    } else {
      trace('ui-sample', { label, ...summarizeUi(ui) });
    }
    if (ui.feed.length >= 2) return ui;
  }
  return readUi(page);
}

let traceIndex = 0;
let browser;
let finalSummary = {
  ok: false,
  artifactsDir,
  tracePath,
};
let resolveBotReady;
const botReady = new Promise((resolve) => {
  resolveBotReady = resolve;
});

try {
  trace('run-start', {
    feBase: FE_BASE,
    directory: DIRECTORY,
    harness: HARNESS,
    model: MODEL,
    title: TITLE,
    headless: HEADLESS,
    cdpPort: CDP_PORT,
  });
  const inputWav = await makeInputAudio();

  trace('browser-launch-start');
  browser = await chromium.launch({
    headless: HEADLESS,
    args: [
      `--remote-debugging-port=${CDP_PORT}`,
      '--remote-debugging-address=127.0.0.1',
      '--remote-allow-origins=*',
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${inputWav}`,
      '--autoplay-policy=no-user-gesture-required',
      '--use-mock-keychain',
      '--password-store=basic',
    ],
  });
  trace('browser-launch-complete', { cdpUrl: `http://127.0.0.1:${CDP_PORT}` });

  const context = await browser.newContext({
    permissions: ['microphone'],
    viewport: { width: 1440, height: 1000 },
  });
  await context.grantPermissions(['microphone'], { origin: FE_BASE });
  await context.addInitScript(() => {
    window.__friday_trace = { ttsActive: false, speakingStart: null };

    const _log = (tag, data) => {
      console.log(`[friday-trace] ${tag}`, JSON.stringify(data ?? {}));
    };

    const O_AC = window.AudioContext || window.webkitAudioContext;
    if (O_AC) {
      // @ts-ignore
      const Orig = O_AC;
      // @ts-ignore
      window.AudioContext = function AudioContext(...args) {
        const ctx = new Orig(...args);
        _log('audiocontext-created', { sampleRate: ctx.sampleRate });
        const origCreateMediaStreamSource = ctx.createMediaStreamSource.bind(ctx);
        ctx.createMediaStreamSource = function (stream) {
          const node = origCreateMediaStreamSource(stream);
          const audioTracks = stream.getAudioTracks();
          if (audioTracks.length > 0) {
            _log('media-stream-source-created', {
              trackCount: audioTracks.length,
              trackLabels: audioTracks.map((t) => t.label),
            });
          }
          return node;
        };
        const origCreateBufferSource = ctx.createBufferSource.bind(ctx);
        ctx.createBufferSource = function () {
          const node = origCreateBufferSource();
          const origStart = node.start.bind(node);
          node.start = function (...startArgs) {
            window.__friday_trace.ttsActive = true;
            window.__friday_trace.speakingStart = Date.now();
            _log('bot-speaking-start', { duration: node.buffer?.duration });
            node.addEventListener('ended', () => {
              window.__friday_trace.ttsActive = false;
              const dur = window.__friday_trace.speakingStart
                ? Date.now() - window.__friday_trace.speakingStart
                : null;
              window.__friday_trace.speakingStart = null;
              _log('bot-speaking-end', { durationMs: dur });
            });
            return origStart(...startArgs);
          };
          return node;
        };
        return ctx;
      };
      // @ts-ignore
      window.AudioContext.prototype = Orig.prototype;
    }

    const O_RTCPC = window.RTCPeerConnection;
    if (O_RTCPC) {
      // @ts-ignore
      window.RTCPeerConnection = function RTCPeerConnection(...args) {
        const pc = new O_RTCPC(...args);
        pc.addEventListener('track', (e) => {
          if (e.track.kind === 'audio') {
            _log('rtc-audio-track-added', { label: e.track.label });
            e.track.addEventListener('ended', () =>
              _log('rtc-audio-track-ended', { label: e.track.label }),
            );
          }
        });
        return pc;
      };
    }

    const origAudioPlay = HTMLAudioElement.prototype.play;
    HTMLAudioElement.prototype.play = function (...args) {
      _log('audio-element-play', { src: this.src?.slice(0, 120), currentTime: this.currentTime });
      return origAudioPlay.apply(this, args);
    };
  });
  const page = await context.newPage();

  page.on('console', (msg) => {
    const text = msg.text();
    trace('browser-console', { level: msg.type(), text });
    const rtviType = /\[RTVI Message\].*type: ([^,}\s]+)/.exec(text)?.[1];
    if (rtviType) trace('rtvi-message', { type: rtviType, raw: text });
    if (text.includes('[Pipecat Client] Bot is ready')) {
      trace('bot-ready-console', { text });
      resolveBotReady();
    }
    const traceMatch = /\[friday-trace\] (\S+)\s+(.+)/.exec(text);
    if (traceMatch) {
      const event = traceMatch[1];
      let data = {};
      try { data = JSON.parse(traceMatch[2]); } catch { /* raw string */ }
      trace('bot-audio', { event, ...data });
    }
  });
  page.on('pageerror', (err) => {
    trace('browser-pageerror', { message: err.message, stack: err.stack });
  });
  page.on('websocket', (ws) => {
    const counters = { sent: 0, received: 0, sentBytes: 0, receivedBytes: 0 };
    trace('websocket-open', { url: ws.url(), counters: { ...counters } });
    ws.on('framesent', (frame) => {
      counters.sent += 1;
      counters.sentBytes += frame.payload.length;
      if (counters.sent <= 10 || counters.sent % 10 === 0) {
        trace('websocket-frame-sent', { url: ws.url(), counters: { ...counters } });
      }
    });
    ws.on('framereceived', (frame) => {
      counters.received += 1;
      counters.receivedBytes += frame.payload.length;
      if (counters.received <= 10 || counters.received % 10 === 0) {
        trace('websocket-frame-received', { url: ws.url(), counters: { ...counters } });
      }
    });
    ws.on('close', () => {
      trace('websocket-close', { url: ws.url(), counters: { ...counters } });
    });
    ws.on('socketerror', (err) => {
      trace('websocket-error', { url: ws.url(), message: String(err), counters: { ...counters } });
    });
  });

  if (EXISTING_SESSION) {
    const url = `${FE_BASE}/s/${EXISTING_SESSION}`;
    trace('navigate-existing-session', { url });
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    trace('navigate-complete', summarizeUi(await readUi(page)));
    await screenshot(page, 'existing-session');
  } else {
    trace('navigate-start', { url: FE_BASE });
    await page.goto(FE_BASE, { waitUntil: 'domcontentloaded' });
    trace('navigate-complete', summarizeUi(await readUi(page)));
    await screenshot(page, 'home');

    trace('click-new-session-start');
    await page.getByRole('button', { name: /new session/i }).click();
    trace('click-new-session-complete');

    trace('select-harness-start', { harness: HARNESS });
    await page.locator('select').nth(0).selectOption(HARNESS);
    trace('select-harness-complete', { harness: HARNESS });

    trace('fill-title-start', { title: TITLE });
    await page.getByPlaceholder('optional').fill(TITLE);
    trace('fill-title-complete', { title: TITLE });

    trace('fill-directory-start', { directory: DIRECTORY });
    await page.getByPlaceholder('/absolute/path').fill(DIRECTORY);
    trace('fill-directory-complete', { directory: DIRECTORY });

    trace('select-model-start', { model: MODEL });
    await page.locator('select').nth(1).selectOption(MODEL);
    trace('select-model-complete', { model: MODEL });
    await screenshot(page, 'modal-selected');

    trace('click-start-session-start');
    await page.getByRole('button', { name: /start session/i }).click();
    await page.waitForURL(/\/s\/new|\/s\/[^/]+$/, { timeout: 20_000 });
    trace('click-start-session-complete', summarizeUi(await readUi(page)));
  }

  trace('wait-record-start-button-start');
  await page.getByRole('button', { name: /^Start/i }).waitFor({ timeout: 25_000 });
  trace('wait-record-start-button-complete', summarizeUi(await readUi(page)));
  trace('wait-bot-ready-start');
  await Promise.race([
    botReady,
    page.waitForTimeout(15_000).then(() => {
      throw new Error('Timed out waiting for Pipecat bot-ready');
    }),
  ]);
  trace('wait-bot-ready-complete', summarizeUi(await readUi(page)));
  await screenshot(page, 'before-record-start');

  const startBtnCount = await page.getByRole('button', { name: /^Start/i }).count();
  if (startBtnCount > 0) {
    trace('click-record-start-start');
    await page.getByRole('button', { name: /^Start/i }).click();
    trace('click-record-start-complete', summarizeUi(await readUi(page)));
    await screenshot(page, 'after-record-start');
  } else {
    trace('recording-auto-started', summarizeUi(await readUi(page)));
  }

  const preSendUi = await sampleLoop(page, 'recording-before-send', WAIT_AFTER_START_MS);
  await screenshot(page, 'before-send');

  trace('click-send-start', summarizeUi(preSendUi));
  try {
    await page.getByRole('button', { name: /^Send/i }).click({ timeout: 5_000 });
    trace('click-send-complete', summarizeUi(await readUi(page)));
  } catch (err) {
    trace('click-send-error', {
      message: err.message,
      visibleUi: summarizeUi(await readUi(page)),
    });
  }

  const finalUi = await sampleLoop(page, 'after-send', WAIT_AFTER_SEND_MS);
  await screenshot(page, 'final');
  const model = modelParts(MODEL);
  finalSummary = {
    ok: finalUi.feed.length >= 2,
    artifactsDir,
    tracePath,
    url: finalUi.url,
    sessionId: /\/s\/([^/?#]+)/.exec(finalUi.url)?.[1] ?? null,
    task: TASK,
    harness: HARNESS,
    model,
    finalUi,
  };
  trace('run-complete', { ok: finalSummary.ok, ...summarizeUi(finalUi) });
} catch (err) {
  trace('run-error', { message: err.message, stack: err.stack });
  finalSummary = {
    ok: false,
    artifactsDir,
    tracePath,
    error: err.message,
  };
} finally {
  await fsp.writeFile(
    path.join(artifactsDir, 'summary.json'),
    `${JSON.stringify(finalSummary, null, 2)}\n`,
  );
  if (browser) await browser.close().catch(() => {});
  trace('browser-closed');
  await new Promise((resolve) => traceStream.end(resolve));
  console.log(JSON.stringify(finalSummary, null, 2));
}
