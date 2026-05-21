import { execFile } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { promisify } from "node:util";
import { chromium } from "playwright";

const execFileP = promisify(execFile);

const { values: args } = parseArgs({
  options: {
    "api-base-url": { type: "string" },
    "cdp-port": { type: "string" },
    directory: { type: "string" },
    "fe-base-url": { type: "string" },
    "hold-ms": { type: "string" },
    harness: { type: "string" },
    headless: { type: "boolean" },
    "leading-silence-seconds": { type: "string" },
    mode: { type: "string" },
    model: { type: "string" },
    output: { type: "string" },
    scenario: { type: "string" },
    "session-id": { type: "string" },
    "session-url": { type: "string" },
    task: { type: "string" },
    "text-channel": { type: "string" },
    title: { type: "string" },
    turn: { type: "string", multiple: true },
    voice: { type: "string" },
    "wait-after-release-ms": { type: "string" },
    "wait-agent-ms": { type: "string" },
  },
  strict: true,
});

const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname);
const WEB_DIR = path.resolve(SCRIPT_DIR, "..");
const REPO_ROOT = path.resolve(WEB_DIR, "..");

const FE_BASE =
  args["fe-base-url"] ?? process.env.FE_BASE_URL ?? "http://localhost:5173";
const API_BASE =
  args["api-base-url"] ?? process.env.API_BASE_URL ?? "http://localhost:8000";
const DIRECTORY = args.directory ?? process.env.PROBE_DIRECTORY ?? REPO_ROOT;
const HARNESS = args.harness ?? process.env.PROBE_HARNESS ?? "opencode";
const MODEL = args.model ?? process.env.PROBE_MODEL ?? "minimax-m2.5-free";
const MODE = args.mode ?? process.env.PROBE_MODE ?? "voice";
if (!["voice", "text"].includes(MODE)) {
  throw new Error(`Unsupported mode: ${MODE}. Expected "voice" or "text".`);
}
const SCENARIO = args.scenario ?? process.env.PROBE_SCENARIO ?? null;
const TITLE =
  args.title ??
  process.env.PROBE_TITLE ??
  `self-test-${new Date().toISOString().replace(/[:.]/g, "")}`;
const DEFAULT_TASK =
  MODE === "text"
    ? "Reply with exactly: Text path works."
    : "Friday, this is your self test. Reply with one short sentence confirming the LiveKit voice path works.";
const TASK = args.task ?? process.env.PROBE_TASK ?? DEFAULT_TASK;
const TEXT_TURNS = resolveTextTurns();
const TEXT_CHANNEL = args["text-channel"] ?? process.env.PROBE_TEXT_CHANNEL ?? "agent";
if (!["agent", "direct", "both"].includes(TEXT_CHANNEL)) {
  throw new Error(
    `Unsupported text channel: ${TEXT_CHANNEL}. Expected "agent", "direct", or "both".`,
  );
}
const ARTIFACTS_ROOT =
  args.output ??
  process.env.PROBE_OUTPUT ??
  path.resolve(REPO_ROOT, "artifacts/self-tests");
const HEADLESS = args.headless || process.env.HEADLESS === "1";
const CDP_PORT = Number(args["cdp-port"] ?? process.env.CDP_PORT ?? 9234);
const LEADING_SILENCE_SECONDS = Number(
  args["leading-silence-seconds"] ?? process.env.LEADING_SILENCE_SECONDS ?? 2,
);
const HOLD_MS = Number(args["hold-ms"] ?? process.env.HOLD_MS ?? 10_000);
const WAIT_AGENT_MS = Number(
  args["wait-agent-ms"] ?? process.env.WAIT_AGENT_MS ?? 30_000,
);
const WAIT_AFTER_RELEASE_MS = Number(
  args["wait-after-release-ms"] ?? process.env.WAIT_AFTER_RELEASE_MS ?? 90_000,
);
const VOICE = args.voice ?? process.env.SAY_VOICE ?? "Samantha";
const SESSION_URL =
  args["session-url"] ??
  (args["session-id"]
    ? `${FE_BASE.replace(/\/$/, "")}/sessions/${args["session-id"]}`
    : null);

const runId = new Date().toISOString().replace(/[:.]/g, "");
const artifactsDir = path.join(ARTIFACTS_ROOT, `${runId}-${slug(TITLE)}`);
const screenshotsDir = path.join(artifactsDir, "screenshots");
await fsp.mkdir(screenshotsDir, { recursive: true });

const tracePath = path.join(artifactsDir, "timeline.jsonl");
const traceStream = fs.createWriteStream(tracePath, { flags: "a" });
const startedAtNs = process.hrtime.bigint();
const startedAtWall = Date.now();
const observed = {
  agentReady: false,
  connected: false,
  error: false,
  finalText: false,
  openedTurn: false,
  sentTurn: false,
  transcriptText: false,
  userText: false,
};
let requiredFinalMessageCount = 0;
let requiredFridayMessageCount = 0;
let requiredTranscriptMessageCount = 0;
let requiredUserMessageCount = 0;

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
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);
}

function resolveTextTurns() {
  if (Array.isArray(args.turn) && args.turn.length > 0) {
    return args.turn;
  }
  if (process.env.PROBE_TURNS) {
    const parsed = JSON.parse(process.env.PROBE_TURNS);
    if (
      !Array.isArray(parsed) ||
      parsed.some((turn) => typeof turn !== "string")
    ) {
      throw new Error("PROBE_TURNS must be a JSON array of strings.");
    }
    return parsed;
  }
  if (SCENARIO === "multi-turn") {
    return [
      TASK,
      "In one short sentence, say what I asked you to verify in the previous turn.",
    ];
  }
  return [TASK];
}

async function healthCheck() {
  const response = await fetch(`${API_BASE}/healthz`);
  if (!response.ok) {
    throw new Error(
      `API health check failed: ${response.status} ${response.statusText}`,
    );
  }
  trace("api-health-ok", { apiBase: API_BASE });
}

async function fetchJson(url) {
  const response = await fetch(url);
  const text = await response.text();
  let body;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!response.ok) {
    throw new Error(
      `GET ${url} failed: ${response.status} ${response.statusText}`,
    );
  }
  return body;
}

async function writeJsonArtifact(name, payload) {
  const target = path.join(artifactsDir, name);
  await fsp.writeFile(target, `${JSON.stringify(payload, null, 2)}\n`);
  trace("artifact-write", { name, path: target });
  return target;
}

async function resolveCommand(name, candidates, probeArgs) {
  for (const candidate of candidates.filter(Boolean)) {
    try {
      await execFileP(candidate, probeArgs, { timeout: 5_000 });
      return candidate;
    } catch {
      // Try the next candidate.
    }
  }
  throw new Error(
    `Unable to find ${name}. Tried: ${candidates.filter(Boolean).join(", ")}`,
  );
}

async function makeInputAudio() {
  const say = await resolveCommand(
    "say",
    [process.env.SAY_PATH, "/usr/bin/say", "say"],
    ["-v", "?"],
  );
  const ffmpeg = await resolveCommand(
    "ffmpeg",
    [
      process.env.FFMPEG_PATH,
      "/opt/homebrew/bin/ffmpeg",
      "/usr/local/bin/ffmpeg",
      "ffmpeg",
    ],
    ["-version"],
  );

  const aiff = path.join(artifactsDir, "input.aiff");
  const speechWav = path.join(artifactsDir, "speech.wav");
  const inputWav = path.join(artifactsDir, "input.wav");

  trace("audio-generate-start", {
    ffmpeg,
    leadingSilenceSeconds: LEADING_SILENCE_SECONDS,
    say,
    task: TASK,
    voice: VOICE,
  });

  await execFileP(say, ["-v", VOICE, "-o", aiff, TASK]);
  await execFileP(ffmpeg, [
    "-y",
    "-hide_banner",
    "-loglevel",
    "error",
    "-i",
    aiff,
    "-ac",
    "2",
    "-ar",
    "44100",
    "-sample_fmt",
    "s16",
    speechWav,
  ]);
  await execFileP(ffmpeg, [
    "-y",
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "lavfi",
    "-i",
    `anullsrc=r=44100:cl=stereo:d=${LEADING_SILENCE_SECONDS}`,
    "-i",
    speechWav,
    "-filter_complex",
    "[0:a][1:a]concat=n=2:v=0:a=1",
    "-ac",
    "2",
    "-ar",
    "44100",
    "-sample_fmt",
    "s16",
    inputWav,
  ]);
  trace("audio-generate-complete", { aiff, inputWav, speechWav });
  return inputWav;
}

async function screenshot(page, name) {
  const filename = `${String(screenshot.index++).padStart(2, "0")}-${name}.png`;
  const target = path.join(screenshotsDir, filename);
  await page.screenshot({ path: target, fullPage: true });
  trace("screenshot", { name, path: target });
  return target;
}
screenshot.index = 0;

async function readUi(page) {
  const ui = await page.evaluate(() => {
    const feed = Array.from(
      document.querySelectorAll(".friday-room__feed-item"),
    ).map((item) => item.textContent?.replace(/\s+/g, " ").trim() ?? "");
    const finalMessages = Array.from(
      document.querySelectorAll(
        '.friday-room__feed-item[data-kind="text_final"]',
      ),
    ).map((item) => item.textContent?.replace(/\s+/g, " ").trim() ?? "");
    const fridayMessages = Array.from(
      document.querySelectorAll('.friday-room__feed-item[data-role="friday"]'),
    ).map((item) => item.textContent?.replace(/\s+/g, " ").trim() ?? "");
    const transcriptMessages = Array.from(
      document.querySelectorAll(
        '.friday-room__feed-item[data-kind="transcript"]',
      ),
    ).map((item) => item.textContent?.replace(/\s+/g, " ").trim() ?? "");
    const userMessages = Array.from(
      document.querySelectorAll('.friday-room__feed-item[data-role="user"]'),
    ).map((item) => item.textContent?.replace(/\s+/g, " ").trim() ?? "");
    const buttons = Array.from(document.querySelectorAll("button")).map(
      (button) => button.textContent?.replace(/\s+/g, " ").trim() ?? "",
    );
    const badges = Array.from(
      document.querySelectorAll(".friday-room__badge, .friday-room__chip"),
    ).map((badge) => badge.textContent?.replace(/\s+/g, " ").trim() ?? "");
    return {
      badges,
      bodyTail: document.body.innerText.slice(-1600),
      buttons,
      feed,
      finalMessages,
      fridayMessages,
      transcriptMessages,
      userMessages,
      url: window.location.href,
    };
  });
  observeUi(ui);
  return ui;
}

function summarizeUi(ui) {
  return {
    badges: ui.badges,
    buttons: ui.buttons,
    feedCount: ui.feed.length,
    feedTail: ui.feed.slice(0, 6),
    finalMessages: ui.finalMessages,
    fridayMessages: ui.fridayMessages,
    transcriptMessages: ui.transcriptMessages,
    userMessages: ui.userMessages,
    url: ui.url,
  };
}

function observeUi(ui) {
  const haystack = `${ui.badges.join("\n")}\n${ui.bodyTail}\n${ui.feed.join("\n")}`;
  observed.agentReady ||=
    /Agent\s+READY|agent ready|agent-[A-Za-z0-9_-]+/i.test(haystack);
  observed.connected ||= /\bconnected\b/i.test(haystack);
  observed.openedTurn ||= /Turn opened/i.test(haystack);
  observed.sentTurn ||= /Turn sent/i.test(haystack);
  observed.finalText ||=
    ui.finalMessages.length > requiredFinalMessageCount ||
    (MODE === "text" && ui.fridayMessages.length > requiredFridayMessageCount);
  observed.transcriptText ||=
    ui.transcriptMessages.length > requiredTranscriptMessageCount;
  observed.userText ||= ui.userMessages.length > requiredUserMessageCount;
  observed.error ||=
    /Room error|Microphone error|Unable to/i.test(haystack) ||
    ui.feed.some((entry) => /^System\b/i.test(entry));
}

async function sampleUntil(
  page,
  label,
  predicate,
  timeoutMs,
  intervalMs = 1000,
) {
  const deadline = Date.now() + timeoutMs;
  let previous = null;
  while (Date.now() < deadline) {
    const ui = await readUi(page);
    const current = JSON.stringify({
      badges: ui.badges,
      buttons: ui.buttons,
      feed: ui.feed,
      finalMessages: ui.finalMessages,
      fridayMessages: ui.fridayMessages,
      transcriptMessages: ui.transcriptMessages,
      userMessages: ui.userMessages,
    });
    if (current !== previous) {
      trace("ui-change", { label, ...summarizeUi(ui) });
      previous = current;
    } else {
      trace("ui-sample", { label, ...summarizeUi(ui) });
    }
    if (predicate(ui)) {
      return ui;
    }
    await page.waitForTimeout(intervalMs);
  }
  return readUi(page);
}

async function waitForAgent(page) {
  const agentUi = await sampleUntil(
    page,
    "waiting-for-agent",
    (ui) => /Agent\s+READY|agent ready|agent-[A-Za-z0-9_-]+/i.test(ui.bodyTail),
    WAIT_AGENT_MS,
  );
  trace("agent-wait-complete", summarizeUi(agentUi));
  return agentUi;
}

async function selectOptionOrFirst(select, desired, label) {
  const options = await select.locator("option").evaluateAll((nodes) =>
    nodes.map((node) => ({
      label: node.textContent?.trim() ?? "",
      value: node.getAttribute("value") ?? "",
    })),
  );
  const normalizedDesired = normalizeModelId(desired);
  const match = options.find(
    (option) => option.value === normalizedDesired || option.value === desired,
  );
  if (match) {
    await select.selectOption(match.value);
    trace("select-option", { label, selected: match.value });
    return match.value;
  }

  const fallback = options.find((option) => option.value);
  if (!fallback) {
    throw new Error(`No selectable ${label} options were available.`);
  }

  await select.selectOption(fallback.value);
  trace("select-option-fallback", {
    available: options,
    label,
    requested: desired,
    selected: fallback.value,
  });
  return fallback.value;
}

function normalizeModelId(value) {
  const slash = value.indexOf("/");
  return slash >= 0 ? value.slice(slash + 1) : value;
}

async function fillCreateSessionForm(page) {
  trace("click-new-session-start");
  await page.getByRole("button", { name: /new session/i }).click();
  trace("click-new-session-complete");

  trace("fill-title-start", { title: TITLE });
  await page.getByLabel(/^title$/i).fill(TITLE);
  trace("fill-title-complete", { title: TITLE });

  trace("fill-directory-start", { directory: DIRECTORY });
  await page.getByPlaceholder("/absolute/path").fill(DIRECTORY);
  trace("fill-directory-complete", { directory: DIRECTORY });

  await selectOptionOrFirst(
    page.locator('label:has-text("harness") select'),
    HARNESS,
    "harness",
  );
  await page.waitForTimeout(250);
  await selectOptionOrFirst(
    page.locator('label:has-text("model") select'),
    MODEL,
    "model",
  );
  await screenshot(page, "modal-ready");

  trace("click-create-session-start");
  await page.getByRole("button", { name: /^create$/i }).click();
  await page.waitForURL(/\/sessions\/[^/]+$/, { timeout: 30_000 });
  trace("click-create-session-complete", summarizeUi(await readUi(page)));
}

async function holdToTalk(page) {
  const talkButton = page.getByRole("button", { name: /^start$/i });
  await talkButton.waitFor({ state: "visible", timeout: 30_000 });
  await expectEnabled(talkButton, 30_000);

  await waitForAgent(page);
  await screenshot(page, "before-hold");

  trace("hold-start", { holdMs: HOLD_MS });
  await talkButton.click();
  await page.waitForTimeout(750);
  observed.openedTurn = true;
  trace("hold-opened", summarizeUi(await readUi(page)));
  await page.waitForTimeout(HOLD_MS);
  await page.getByRole("button", { name: /^send$/i }).click();
  observed.sentTurn = true;
  trace("hold-complete", summarizeUi(await readUi(page)));
  await screenshot(page, "after-release");
}

async function setSpeakerOff(page) {
  const speakerOn = page.getByRole("button", { name: /^speaker:\s*on$/i });
  if ((await speakerOn.count()) === 0) {
    trace("speaker-already-off");
    return;
  }
  await speakerOn.click();
  await sampleUntil(
    page,
    "waiting-for-speaker-off",
    (ui) => ui.buttons.some((button) => /^speaker:\s*off$/i.test(button)),
    5_000,
    250,
  );
  trace("speaker-off");
}

async function submitTextTurn(page, text, index, channel) {
  const messageBox = page.getByLabel("Message Friday");
  await messageBox.waitFor({ state: "visible", timeout: 30_000 });
  await messageBox.fill(text);
  const sendButton =
    channel === "direct"
      ? page.getByRole("menuitem", { name: /^direct api$/i })
      : page.getByRole("button", { name: /^send via agent$/i });
  if (channel === "direct") {
    await page.getByLabel("Text send options").click();
  }
  await expectEnabled(sendButton, 10_000);

  const baselineUi = await readUi(page);
  const turnBaselineFinalCount = baselineUi.finalMessages.length;
  const turnBaselineFridayCount = baselineUi.fridayMessages.length;
  const turnBaselineUserCount = baselineUi.userMessages.length;
  trace("text-turn-start", {
    channel,
    index,
    finalMessageCount: turnBaselineFinalCount,
    fridayMessageCount: turnBaselineFridayCount,
    text,
    userMessageCount: turnBaselineUserCount,
  });

  await sendButton.click();
  observed.sentTurn = true;
  observed.userText = false;
  observed.finalText = false;

  const finalUi = await sampleUntil(
    page,
    `waiting-for-text-final-${index}`,
    (ui) =>
      ui.finalMessages.length > turnBaselineFinalCount ||
      ui.fridayMessages.length > turnBaselineFridayCount ||
      ui.feed.some((entry) => /^System\b/i.test(entry)),
    WAIT_AFTER_RELEASE_MS,
  );
  trace("text-turn-complete", {
    channel,
    index,
    ...summarizeUi(finalUi),
    userTextVisible: finalUi.userMessages.length > turnBaselineUserCount,
  });
  return finalUi;
}

async function runTextTurns(page) {
  await waitForAgent(page);
  await setSpeakerOff(page);
  await screenshot(page, "before-text-turns");
  let ui = await readUi(page);
  const channels =
    TEXT_CHANNEL === "both" ? ["agent", "direct"] : [TEXT_CHANNEL];
  const turns =
    TEXT_CHANNEL === "both" && TEXT_TURNS.length === 1
      ? [TEXT_TURNS[0], TEXT_TURNS[0]]
      : TEXT_TURNS;
  for (let index = 0; index < turns.length; index += 1) {
    const channel = channels[index % channels.length];
    ui = await submitTextTurn(page, turns[index], index, channel);
    await screenshot(page, `text-turn-${index + 1}`);
  }
  return ui;
}

async function expectEnabled(locator, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await locator.isEnabled()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    "Timed out waiting for talk button to become enabled.",
  );
}

function extractSessionId(url) {
  return /\/sessions\/([^/?#]+)/.exec(url)?.[1] ?? null;
}

async function collectApiArtifacts(sessionId) {
  if (!sessionId) {
    return {
      paths: {},
      sessionDetail: null,
      narratorEvents: null,
    };
  }
  const sessionDetail = await fetchJson(
    `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`,
  );
  const narratorEvents = await fetchJson(
    `${API_BASE}/api/narrator/sessions/${encodeURIComponent(
      sessionId,
    )}/events?after_id=0&limit=100`,
  );
  const paths = {
    narratorEvents: await writeJsonArtifact(
      "narrator-events.json",
      narratorEvents,
    ),
    sessionDetail: await writeJsonArtifact(
      "session-detail.json",
      sessionDetail,
    ),
  };
  return {
    paths,
    sessionDetail,
    narratorEvents,
  };
}

function evaluateApiArtifacts(apiArtifacts) {
  const expectedTurnCount = MODE === "text" ? TEXT_TURNS.length : 1;
  const events = apiArtifacts.narratorEvents?.events ?? [];
  const transcript = apiArtifacts.sessionDetail?.transcript ?? [];
  const narratorTranscript =
    apiArtifacts.sessionDetail?.narrator_transcript ?? [];
  const providerAssistantFinals = transcript.filter(
    (entry) => entry.role === "assistant" && entry.text?.trim() && !entry.error,
  );
  const narratorFinalEvents = events.filter(
    (event) => event.type === "final" && event.text?.trim(),
  );
  const errorEvents = events.filter((event) => event.type === "error");
  return {
    apiAgentIdle: apiArtifacts.sessionDetail?.agent_state === "idle",
    apiHasError: errorEvents.length > 0,
    apiNarratorFinal: narratorFinalEvents.length >= expectedTurnCount,
    apiNarratorTranscript: narratorTranscript.length >= TEXT_TURNS.length * 2,
    apiProviderFinal: providerAssistantFinals.length >= expectedTurnCount,
    currentModel: apiArtifacts.sessionDetail?.current_model ?? null,
    errorEvents: errorEvents.map((event) => ({
      id: event.id,
      text: event.text,
    })),
    narratorFinalEvents: narratorFinalEvents.map((event) => ({
      id: event.id,
      text: event.text,
    })),
    providerAssistantFinals: providerAssistantFinals.map((entry) => ({
      completed_at: entry.completed_at,
      model: entry.model,
      text: entry.text,
    })),
  };
}

function evaluateRun(ui, apiChecks = {}) {
  observeUi(ui);
  const agentReady = observed.agentReady;
  const hasConnected = observed.connected;
  const openedTurn = observed.openedTurn;
  const sentTurn = observed.sentTurn;
  const finalText = observed.finalText;
  const hasError = observed.error;
  const transcriptText = observed.transcriptText;
  const userText = observed.userText;

  if (MODE === "text") {
    const apiOk =
      apiChecks.apiNarratorFinal &&
      apiChecks.apiProviderFinal &&
      apiChecks.apiAgentIdle &&
      !apiChecks.apiHasError;
    return {
      agentReady,
      apiAgentIdle: Boolean(apiChecks.apiAgentIdle),
      apiHasError: Boolean(apiChecks.apiHasError),
      apiNarratorFinal: Boolean(apiChecks.apiNarratorFinal),
      apiProviderFinal: Boolean(apiChecks.apiProviderFinal),
      finalText,
      hasConnected,
      hasError,
      ok:
        hasConnected &&
        agentReady &&
        sentTurn &&
        userText &&
        finalText &&
        apiOk &&
        !hasError,
      sentTurn,
      userText,
    };
  }

  return {
    agentReady,
    hasConnected,
    hasError,
    ok:
      hasConnected &&
      openedTurn &&
      sentTurn &&
      transcriptText &&
      finalText &&
      !hasError,
    openedTurn,
    sentTurn,
    finalText,
    transcriptText,
  };
}

let browser;
let finalSummary = {
  ok: false,
  artifactsDir,
  tracePath,
};

try {
  trace("run-start", {
    apiBase: API_BASE,
    cdpPort: CDP_PORT,
    directory: DIRECTORY,
    feBase: FE_BASE,
    harness: HARNESS,
    headless: HEADLESS,
    holdMs: HOLD_MS,
    mode: MODE,
    model: MODEL,
    scenario: SCENARIO,
    textChannel: TEXT_CHANNEL,
    textTurns: TEXT_TURNS,
    title: TITLE,
  });
  await healthCheck();
  const inputWav = MODE === "voice" ? await makeInputAudio() : null;

  trace("browser-launch-start");
  browser = await chromium.launch({
    headless: HEADLESS,
    args: [
      `--remote-debugging-port=${CDP_PORT}`,
      "--remote-debugging-address=127.0.0.1",
      "--remote-allow-origins=*",
      ...(MODE === "voice"
        ? [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            `--use-file-for-fake-audio-capture=${inputWav}`,
          ]
        : []),
      "--autoplay-policy=no-user-gesture-required",
      "--use-mock-keychain",
      "--password-store=basic",
    ],
  });
  trace("browser-launch-complete", { cdpUrl: `http://127.0.0.1:${CDP_PORT}` });

  const context = await browser.newContext({
    permissions: ["microphone"],
    viewport: { width: 390, height: 844 },
  });
  await context.grantPermissions(["microphone"], { origin: FE_BASE });
  const page = await context.newPage();

  page.on("console", (msg) => {
    trace("browser-console", { level: msg.type(), text: msg.text() });
  });
  page.on("pageerror", (err) => {
    trace("browser-pageerror", { message: err.message, stack: err.stack });
  });
  page.on("requestfailed", (request) => {
    trace("browser-request-failed", {
      failure: request.failure()?.errorText,
      method: request.method(),
      url: request.url(),
    });
  });
  page.on("websocket", (ws) => {
    const counters = { received: 0, sent: 0 };
    trace("websocket-open", { counters: { ...counters }, url: ws.url() });
    ws.on("framesent", () => {
      counters.sent += 1;
      if (counters.sent <= 10 || counters.sent % 25 === 0) {
        trace("websocket-frame-sent", {
          counters: { ...counters },
          url: ws.url(),
        });
      }
    });
    ws.on("framereceived", () => {
      counters.received += 1;
      if (counters.received <= 10 || counters.received % 25 === 0) {
        trace("websocket-frame-received", {
          counters: { ...counters },
          url: ws.url(),
        });
      }
    });
    ws.on("close", () => {
      trace("websocket-close", { counters: { ...counters }, url: ws.url() });
    });
  });

  const startUrl = SESSION_URL ?? FE_BASE;
  trace("navigate-start", { url: startUrl });
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  trace("navigate-complete", summarizeUi(await readUi(page)));
  await screenshot(page, "home");

  if (!SESSION_URL) {
    await fillCreateSessionForm(page);
  }
  const baselineUi = await readUi(page);
  requiredFinalMessageCount = baselineUi.finalMessages.length;
  requiredFridayMessageCount = baselineUi.fridayMessages.length;
  requiredTranscriptMessageCount = baselineUi.transcriptMessages.length;
  requiredUserMessageCount = baselineUi.userMessages.length;
  observed.finalText = false;
  observed.transcriptText = false;
  observed.userText = false;
  trace("turn-baseline", {
    finalMessageCount: requiredFinalMessageCount,
    fridayMessageCount: requiredFridayMessageCount,
    transcriptMessageCount: requiredTranscriptMessageCount,
    userMessageCount: requiredUserMessageCount,
    url: baselineUi.url,
  });

  let finalUi;
  if (MODE === "text") {
    finalUi = await runTextTurns(page);
  } else {
    await holdToTalk(page);
    finalUi = await sampleUntil(
      page,
      "waiting-for-final-response",
      (ui) =>
        ui.finalMessages.length > requiredFinalMessageCount ||
        ui.feed.some((entry) => /^System\b/i.test(entry)),
      WAIT_AFTER_RELEASE_MS,
    );
  }
  await screenshot(page, "final");

  const sessionId = extractSessionId(finalUi.url);
  const apiArtifacts = await collectApiArtifacts(sessionId);
  const apiChecks = evaluateApiArtifacts(apiArtifacts);
  const checks = evaluateRun(finalUi, apiChecks);
  finalSummary = {
    ok: checks.ok,
    artifactsDir,
    apiArtifacts: apiArtifacts.paths,
    apiChecks,
    checks,
    directory: DIRECTORY,
    finalUi,
    harness: HARNESS,
    mode: MODE,
    model: normalizeModelId(MODEL),
    scenario: SCENARIO,
    sessionId,
    task: TASK,
    textChannel: MODE === "text" ? TEXT_CHANNEL : null,
    turns: MODE === "text" ? TEXT_TURNS : [TASK],
    tracePath,
    url: finalUi.url,
  };
  trace("run-complete", {
    ok: finalSummary.ok,
    ...checks,
    ...summarizeUi(finalUi),
  });
} catch (err) {
  trace("run-error", { message: err.message, stack: err.stack });
  finalSummary = {
    ok: false,
    artifactsDir,
    error: err.message,
    tracePath,
  };
} finally {
  await fsp.writeFile(
    path.join(artifactsDir, "summary.json"),
    `${JSON.stringify(finalSummary, null, 2)}\n`,
  );
  if (browser) {
    await browser.close().catch(() => {});
  }
  trace("browser-closed");
  await new Promise((resolve) => traceStream.end(resolve));
  console.log(JSON.stringify(finalSummary, null, 2));
  if (!finalSummary.ok) {
    process.exitCode = 1;
  }
}
