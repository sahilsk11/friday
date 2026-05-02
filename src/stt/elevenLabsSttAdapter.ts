// ElevenLabs Realtime STT (Scribe v2 Realtime).
//
// Endpoint:  wss://api.elevenlabs.io/v1/speech-to-text/realtime
//   ?model_id=scribe_v2_realtime
//   &commit_strategy=vad        // auto-commit on silence
//
// Auth:      xi-api-key header
// Send:      { message_type: 'input_audio_chunk', audio_base_64, sample_rate }
// Receive:   { message_type: 'session_started' | 'partial_transcript'
//                            | 'committed_transcript' | 'auth_error' | ... }
//
// The previous adapter targeted /v1/speech-to-text (HTTP file upload, not WS)
// with the wrong auth and message shape — every connection 403'd.

import WebSocket from 'ws';

import type { SttAdapter } from './types.js';
import { logger } from '../logger.js';

interface SttStartOptions {
  sessionId: string;
  language?: string;
  onPartial(text: string): void;
  onFinal(text: string): void;
  onError(error: Error): void;
}

interface ElevenLabsSttMessage {
  message_type: string;
  text?: string;
  error?: string;
}

const MODEL_ID = 'scribe_v2_realtime';
const SAMPLE_RATE = 16000;

function buildEndpointUrl(language?: string): string {
  const params = new URLSearchParams({
    model_id: MODEL_ID,
    commit_strategy: 'vad',
  });
  if (language) {
    params.set('language_code', language);
  }
  return `wss://api.elevenlabs.io/v1/speech-to-text/realtime?${params.toString()}`;
}

export class ElevenLabsSttAdapter implements SttAdapter {
  private apiKey: string;
  private ws?: WebSocket;
  private opened = false;
  private stopped = false;

  constructor(
    apiKey: string,
    _onPartialUnused: (text: string) => void,
    _onFinalUnused: (text: string) => void
  ) {
    // The legacy constructor took partial/final callbacks but they were never
    // used — everything routes through start()'s options. Kept for signature
    // compatibility with sessionManager's sttFactory.
    this.apiKey = apiKey;
  }

  async start(options: SttStartOptions): Promise<void> {
    this.stopped = false;
    this.opened = false;

    const url = buildEndpointUrl(options.language);
    logger.info('ElevenLabs STT: connecting', { sessionId: options.sessionId, modelId: MODEL_ID });

    return new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(url, {
        headers: { 'xi-api-key': this.apiKey },
      });
      this.ws = socket;

      socket.once('open', () => {
        this.opened = true;
        logger.info('ElevenLabs STT: open', { sessionId: options.sessionId });
        resolve();
      });

      socket.once('error', (err: Error) => {
        if (!this.stopped) {
          logger.error('ElevenLabs STT error', { sessionId: options.sessionId, error: err.message });
          options.onError(err);
        }
        if (!this.opened) reject(err);
      });

      socket.on('message', (data: Buffer | string) => {
        let parsed: ElevenLabsSttMessage;
        try {
          parsed = JSON.parse(data.toString()) as ElevenLabsSttMessage;
        } catch {
          return;
        }

        switch (parsed.message_type) {
          case 'session_started':
            break;
          case 'partial_transcript':
            if (parsed.text) options.onPartial(parsed.text);
            break;
          case 'committed_transcript':
          case 'committed_transcript_with_timestamps':
            if (parsed.text) options.onFinal(parsed.text);
            break;
          case 'auth_error':
          case 'quota_exceeded':
          case 'rate_limited':
          case 'input_error':
          case 'transcriber_error':
          case 'error':
            options.onError(new Error(`ElevenLabs STT ${parsed.message_type}: ${parsed.error ?? ''}`));
            break;
          // soft signals (insufficient_audio_activity, commit_throttled, etc.) — ignore
        }
      });

      socket.on('close', (code: number) => {
        logger.info('ElevenLabs STT: closed', { sessionId: options.sessionId, code });
        this.ws = undefined;
        if (!this.opened) reject(new Error(`STT closed before open: code=${code}`));
      });
    });
  }

  sendAudio(chunk: ArrayBuffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const audio_base_64 = Buffer.from(chunk).toString('base64');
    this.ws.send(
      JSON.stringify({
        message_type: 'input_audio_chunk',
        audio_base_64,
        sample_rate: SAMPLE_RATE,
      })
    );
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (!this.ws) return;
    const socket = this.ws;
    this.ws = undefined;

    if (socket.readyState !== WebSocket.OPEN) {
      socket.close();
      return;
    }

    // Send a final commit chunk and wait briefly for committed_transcript so
    // we don't cut off the user's last words.
    await new Promise<void>((resolve) => {
      let resolved = false;
      const finish = (): void => {
        if (resolved) return;
        resolved = true;
        socket.close();
        resolve();
      };

      socket.on('message', (data: Buffer | string) => {
        try {
          const parsed = JSON.parse(data.toString()) as { message_type?: string };
          if (
            parsed.message_type === 'committed_transcript' ||
            parsed.message_type === 'committed_transcript_with_timestamps'
          ) {
            setTimeout(finish, 50);
          }
        } catch {
          /* ignore */
        }
      });

      try {
        socket.send(
          JSON.stringify({
            message_type: 'input_audio_chunk',
            audio_base_64: '',
            commit: true,
            sample_rate: SAMPLE_RATE,
          })
        );
      } catch {
        finish();
        return;
      }

      setTimeout(finish, 1500);
    });
  }
}
