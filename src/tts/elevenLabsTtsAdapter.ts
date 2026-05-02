import WebSocket from 'ws';

import type { TtsAdapter } from './types.js';
import { logger } from '../logger.js';

interface TtsStartOptions {
  sessionId: string;
  turnId: string;
  voiceId: string;
  modelId: string;
  onChunk(chunk: { sequence: number; audioBase64: string; mimeType: 'audio/mpeg' | 'audio/pcm' }): void;
  onStart(): void;
  onEnd(): void;
  onError(error: Error): void;
}

interface ElevenLabsTtsMessage {
  type: string;
  audio?: string;
}

export class ElevenLabsTtsAdapter implements TtsAdapter {
  private apiKey: string;
  private ws?: WebSocket;
  private onChunkHandler: (chunk: { sequence: number; audioBase64: string; mimeType: 'audio/mpeg' | 'audio/pcm' }) => void;
  private onStartHandler: () => void;
  private onEndHandler: () => void;
  private onErrorHandler: (error: Error) => void;
  private sessionId: string;
  private turnId: string;
  private sequence = 0;
  private textBuffer = '';
  private isStarted = false;
  // Set when stop() is called before the WS has opened. The 'open' handler
  // checks this and immediately flushes — otherwise stop() races with start().
  private pendingFlush = false;

  constructor(apiKey: string) {
    this.apiKey = apiKey;
    // eslint-disable-next-line @typescript-eslint/no-empty-function
    this.onChunkHandler = () => {};
    this.sessionId = '';
    this.turnId = '';
    // eslint-disable-next-line @typescript-eslint/no-empty-function
    this.onStartHandler = () => {};
    // eslint-disable-next-line @typescript-eslint/no-empty-function
    this.onEndHandler = () => {};
    // eslint-disable-next-line @typescript-eslint/no-empty-function
    this.onErrorHandler = () => {};
  }

  async start(options: TtsStartOptions): Promise<void> {
    this.sessionId = options.sessionId;
    this.turnId = options.turnId;
    this.onChunkHandler = (chunk) => options.onChunk(chunk);
    this.onStartHandler = options.onStart;
    this.onEndHandler = options.onEnd;
    this.onErrorHandler = options.onError;
    this.sequence = 0;

    return new Promise((resolve, reject) => {
      const url = `wss://api.elevenlabs.io/v1/text-to-speech/${options.voiceId}/stream-input`;
      this.ws = new WebSocket(url, { headers: { "xi-api-key": this.apiKey } });

      this.ws.on('open', () => {
        this.isStarted = true;
        logger.info('ElevenLabs TTS connected', { sessionId: options.sessionId, turnId: options.turnId });

        this.ws?.send(
          JSON.stringify({
            text: ' ',
            voice_settings: { stability: 0.5, similarity_boost: 0.8 },
            model_id: options.modelId,
          })
        );

        this.onStartHandler();
        resolve();

        // If stop() was called while we were connecting, flush now that we're
        // open. ElevenLabs will close after sending any buffered audio.
        if (this.pendingFlush) {
          this.pendingFlush = false;
          this.ws?.send(JSON.stringify({ text: '' }));
        }
      });

      this.ws.on('message', (data: WebSocket.Data) => {
        try {
          const message: ElevenLabsTtsMessage = JSON.parse(data.toString());

          if (message.audio) {
            this.sequence++;
            this.onChunkHandler({
              sequence: this.sequence,
              audioBase64: message.audio,
              mimeType: 'audio/mpeg',
            });
          }
        } catch (error) {
          logger.error('Failed to parse TTS message', { error: String(error) });
        }
      });

      this.ws.on('error', (error) => {
        logger.error('ElevenLabs TTS error', { error: error.message });
        this.onErrorHandler(error);
        reject(error);
      });

      this.ws.on('close', () => {
        this.isStarted = false;
        this.onEndHandler();
        logger.info('ElevenLabs TTS disconnected', { sessionId: this.sessionId, turnId: this.turnId });
      });
    });
  }

  sendText(text: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }

    this.textBuffer += text;
    this.ws.send(
      JSON.stringify({
        text: text,
        try_trigger_generation: true,
      })
    );
  }

  flush(): void {
    if (this.textBuffer.trim()) {
      this.sendText('\n');
      this.textBuffer = '';
    }
  }

  async stop(): Promise<void> {
    if (!this.ws) return;
    if (!this.isStarted) {
      // WS still mid-handshake. Flag it; the 'open' handler will flush.
      // Wait here for the close that follows.
      this.pendingFlush = true;
    }
    const ws = this.ws;
    // Empty-text is ElevenLabs's flush signal. Don't call ws.close() ourselves
    // — it races with the audio chunks ElevenLabs is about to send. Let
    // ElevenLabs close once it's done. 5s safety net in case it stalls.
    return new Promise<void>((resolve) => {
      const done = (): void => {
        clearTimeout(timer);
        this.ws = undefined;
        this.isStarted = false;
        resolve();
      };
      const timer = setTimeout(() => {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
        done();
      }, 5000);
      ws.once('close', done);
      // Only send the flush directly if we're already open. If not, the
      // 'open' handler above will send it via pendingFlush.
      if (this.isStarted) {
        ws.send(JSON.stringify({ text: '' }));
      }
    });
  }
}