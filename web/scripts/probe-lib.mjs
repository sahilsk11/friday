import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

export const FE_BASE = process.env.FE_BASE_URL ?? 'http://localhost:5173';
export const FAKE_AUDIO = process.env.FAKE_AUDIO_PATH ?? '/tmp/voice-test-input.wav';
export const DEFAULT_PROBE_DIRECTORY = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../..',
);
export const PROBE_DIRECTORY = process.env.PROBE_DIRECTORY ?? DEFAULT_PROBE_DIRECTORY;
export const PROBE_HARNESS = process.env.PROBE_HARNESS;

export function assertFakeAudio() {
  if (fs.existsSync(FAKE_AUDIO)) return;
  console.error(`fake audio file missing: ${FAKE_AUDIO}`);
  console.error(
    'regenerate with: say "hey friday what does this code do" -o /tmp/voice-test-input.wav --data-format=LEI16@16000',
  );
  process.exit(2);
}

export async function launchFakeMicBrowser() {
  assertFakeAudio();
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
  await ctx.grantPermissions(['microphone'], { origin: FE_BASE });
  return { browser, ctx };
}

export async function createSessionThroughUi(page, title = 'probe-voice') {
  console.log(`[probe] creating session through UI directory=${PROBE_DIRECTORY}`);
  await page.getByRole('button', { name: /new session/i }).click();

  const harnessSelect = page.locator('select').first();
  await harnessSelect.waitFor({ state: 'visible', timeout: 20_000 });
  if (PROBE_HARNESS) {
    await harnessSelect.selectOption(PROBE_HARNESS);
  }

  const titleInput = page.getByPlaceholder('optional');
  const directoryInput = page.getByPlaceholder('/absolute/path');
  await titleInput.fill(title);
  await directoryInput.fill(PROBE_DIRECTORY);

  const startButton = page.getByRole('button', { name: /start session/i });
  await page.waitForFunction(
    () => {
      const input = document.querySelector('input[placeholder="/absolute/path"]');
      const button = Array.from(document.querySelectorAll('button')).find((node) =>
        /start session/i.test(node.textContent ?? ''),
      );
      return (
        input instanceof HTMLInputElement &&
        input.value.trim().length > 0 &&
        button instanceof HTMLButtonElement &&
        !button.disabled
      );
    },
    undefined,
    { timeout: 20_000 },
  );
  await startButton.click();
  await page.waitForURL(/\/s\/new|\/s\/[^/]+$/, { timeout: 20_000 });
}
