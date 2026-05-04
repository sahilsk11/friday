/**
 * ElevenLabs Realtime Speech-to-Text adapter (Scribe v2 Realtime).
 *
 * Wire protocol (verified against the official docs as of 2026-05):
 *
 *  Connection
 *    wss://api.elevenlabs.io/v1/speech-to-text/realtime
 *      ?model_id=scribe_v2_realtime
 *      &commit_strategy=vad         (auto-commit on silence — natural speech UX)
 *    Header: xi-api-key: <key>
 *
 *  Client → Server (JSON)
 *    {
 *      "message_type": "input_audio_chunk",
 *      "audio_base_64": "<base64 PCM16 LE mono>",
 *      "sample_rate": 16000
 *    }
 *
 *  Server → Client (JSON)
 *    { "message_type": "session_started", ... }
 *    { "message_type": "partial_transcript",   "text": "..." }
 *    { "message_type": "committed_transcript", "text": "..." }
 *    { "message_type": "auth_error" | "quota_exceeded" | "input_error" | ..., "error": "..." }
 */

import { WebSocket } from 'ws';

import { config } from '../config.js';
import { logger } from '../logger.js';

export interface SttAdapter {
  start(options: {
    sessionId: string;
    language?: string;
    sampleRate: number;
    onPartial(text: string): void;
    onFinal(text: string): void;
    onError(error: Error): void;
  }): Promise<void>;

  sendAudio(chunk: Uint8Array): void;

  stop(): Promise<void>;
}

interface ElevenLabsSttMessage {
  message_type: string;
  text?: string;
  error?: string;
  [key: string]: unknown;
}

const MODEL_ID = 'scribe_v2_realtime';

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

export function createElevenLabsSttAdapter(): SttAdapter {
  let ws: WebSocket | null = null;
  let stopped = false;
  let sampleRateRef = 16000;

  return {
    async start(options): Promise<void> {
      const { sessionId, language, sampleRate, onPartial, onFinal, onError } = options;
      sampleRateRef = sampleRate;

      const url = buildEndpointUrl(language);
      logger.info('ElevenLabs STT: connecting', { sessionId, modelId: MODEL_ID, sampleRate });

      await new Promise<void>((resolve, reject) => {
        const socket = new WebSocket(url, {
          headers: { 'xi-api-key': config.elevenLabsApiKey },
        });

        ws = socket;
        let opened = false;

        socket.once('open', () => {
          opened = true;
          logger.info('ElevenLabs STT: connection open', { sessionId });
          resolve();
        });

        socket.once('error', (err: Error) => {
          if (!stopped) {
            logger.error('ElevenLabs STT: socket error', { sessionId, err: err.message });
            onError(err);
          }
          if (!opened) reject(err);
        });

        socket.on('message', (data: Buffer | string) => {
          let parsed: ElevenLabsSttMessage;
          try {
            parsed = JSON.parse(data.toString()) as ElevenLabsSttMessage;
          } catch {
            logger.debug('ElevenLabs STT: non-JSON message', { sessionId, data: String(data) });
            return;
          }

          const messageType = parsed.message_type;

          switch (messageType) {
            case 'session_started':
              logger.debug('ElevenLabs STT: session started', { sessionId });
              break;

            case 'partial_transcript': {
              const text = parsed.text ?? '';
              if (text) onPartial(text);
              break;
            }

            case 'committed_transcript':
            case 'committed_transcript_with_timestamps': {
              const text = parsed.text ?? '';
              if (text) onFinal(text);
              break;
            }

            case 'auth_error':
            case 'quota_exceeded':
            case 'unaccepted_terms':
            case 'rate_limited':
            case 'queue_overflow':
            case 'resource_exhausted':
            case 'session_time_limit_exceeded':
            case 'input_error':
            case 'chunk_size_exceeded':
            case 'transcriber_error':
            case 'error': {
              const errMsg = parsed.error ?? messageType;
              onError(new Error(`ElevenLabs STT ${messageType}: ${errMsg}`));
              break;
            }

            case 'insufficient_audio_activity':
            case 'commit_throttled':
              logger.debug('ElevenLabs STT: soft signal', { sessionId, messageType });
              break;

            default:
              logger.debug('ElevenLabs STT: unhandled message_type', {
                sessionId,
                message_type: messageType,
              });
          }
        });

        socket.on('close', (code: number, reason: Buffer) => {
          logger.info('ElevenLabs STT: connection closed', {
            sessionId,
            code,
            reason: reason.toString(),
          });
          ws = null;
          if (!opened) reject(new Error(`STT closed before open: code=${String(code)}`));
        });
      });
    },

    sendAudio(chunk: Uint8Array): void {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        logger.debug('ElevenLabs STT: sendAudio called but socket not open — dropping chunk');
        return;
      }
      const audio_base_64 = Buffer.from(chunk).toString('base64');
      ws.send(
        JSON.stringify({
          message_type: 'input_audio_chunk',
          audio_base_64,
          sample_rate: sampleRateRef,
        }),
      );
    },

    async stop(): Promise<void> {
      stopped = true;
      if (!ws) return;
      const socket = ws;
      ws = null;

      if (socket.readyState !== WebSocket.OPEN) {
        socket.close();
        return;
      }

      // Send a final commit chunk and wait briefly for committed_transcript
      // before closing — otherwise the server cuts off mid-final.
      await new Promise<void>((resolve) => {
        let resolved = false;
        const finish = (): void => {
          if (resolved) return;
          resolved = true;
          socket.close();
          resolve();
        };

        // Listen for one more committed transcript or close, then bail.
        const onMessage = (data: Buffer | string): void => {
          try {
            const parsed = JSON.parse(data.toString()) as { message_type?: string };
            if (
              parsed.message_type === 'committed_transcript' ||
              parsed.message_type === 'committed_transcript_with_timestamps'
            ) {
              setTimeout(finish, 50); // tiny tail-flush
            }
          } catch {
            /* ignore */
          }
        };
        socket.on('message', onMessage);

        try {
          socket.send(
            JSON.stringify({
              message_type: 'input_audio_chunk',
              audio_base_64: '',
              commit: true,
              sample_rate: sampleRateRef,
            }),
          );
        } catch {
          finish();
          return;
        }

        // Hard timeout if server never commits.
        setTimeout(finish, 1500);
      });
    },
  };
}
