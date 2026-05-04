import 'dotenv/config';

import type { RuntimeConfig } from './protocol.js';

export const defaultRuntimeConfig: RuntimeConfig = {
  sttProvider: 'elevenlabs',
  ttsProvider: 'elevenlabs',
  ttsVoiceId: 'EXAVITQu4vr4xnSDxMaL', // Sarah
  ttsModelId: 'eleven_flash_v2_5',
  autoSpeak: true,
  autoSendFinalTranscript: true,
  chunking: { maxChars: 200, maxDelayMs: 250, sentenceBoundary: true },
};

export interface Config {
  elevenLabsApiKey: string;
  opencodeBaseUrl: string;
  port: number;
}

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

function optionalEnv(name: string, fallback: string): string {
  return process.env[name] ?? fallback;
}

export const config: Config = {
  elevenLabsApiKey: requireEnv('ELEVENLABS_API_KEY'),
  opencodeBaseUrl: optionalEnv('OPENCODE_BASE_URL', 'http://127.0.0.1:4096'),
  port: parseInt(optionalEnv('PORT', '8787'), 10),
};
