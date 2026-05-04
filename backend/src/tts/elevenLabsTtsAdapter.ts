import { WebSocket } from 'ws';

import { config } from '../config.js';

export interface TtsAdapter {
  start(options: {
    sessionId: string;
    turnId: string;
    voiceId: string;
    modelId: string;
    onChunk(chunk: Uint8Array, sequence: number): void;
    onStart(): void;
    onEnd(): void;
    onError(error: Error): void;
  }): Promise<void>;

  // Send a piece of text to be spoken.
  sendText(text: string): void;

  // Flush any buffered text to ensure it is spoken.
  flush(): void;

  // Stop speaking and close the WebSocket.
  stop(): Promise<void>;
}

interface ElevenLabsAudioMessage {
  audio?: string;
  isFinal?: boolean;
  message?: string;
  error?: string;
}

type StartOptions = Parameters<TtsAdapter['start']>[0];

class ElevenLabsTtsAdapter implements TtsAdapter {
  private ws: WebSocket | null = null;
  private sequence = 0;
  private started = false;
  private stopResolve: (() => void) | null = null;
  // Buffer of text sent before the WS is open. Replayed on `open`.
  private preOpenQueue: string[] = [];
  private wsOpen = false;

  async start(options: StartOptions): Promise<void> {
    this.sequence = 0;
    this.started = false;

    const { voiceId, modelId } = options;
    const url =
      `wss://api.elevenlabs.io/v1/text-to-speech/${voiceId}/stream-input` +
      `?model_id=${encodeURIComponent(modelId)}&output_format=mp3_44100_64`;

    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(url, {
        headers: { 'xi-api-key': config.elevenLabsApiKey },
      });
      this.ws = ws;

      ws.once('open', () => {
        this.wsOpen = true;
        // Send the initialization message required by ElevenLabs Multi-Stream Input.
        const initMsg = JSON.stringify({ text: ' ' });
        ws.send(initMsg);
        // Replay anything buffered before open.
        while (this.preOpenQueue.length > 0) {
          const buffered = this.preOpenQueue.shift();
          if (buffered === undefined) break;
          ws.send(
            JSON.stringify({ text: `${buffered} `, try_trigger_generation: true }),
          );
        }
        resolve();
      });

      ws.once('error', (err: Error) => {
        if (!this.started) {
          reject(err);
        } else {
          options.onError(err);
        }
      });

      ws.on('message', (data: Buffer | string) => {
        const raw = typeof data === 'string' ? data : data.toString('utf8');
        let msg: ElevenLabsAudioMessage;
        try {
          msg = JSON.parse(raw) as ElevenLabsAudioMessage;
        } catch {
          return;
        }

        if (msg.error) {
          options.onError(new Error(msg.error));
          return;
        }

        if (msg.audio) {
          if (!this.started) {
            this.started = true;
            options.onStart();
          }
          const bytes = Buffer.from(msg.audio, 'base64');
          options.onChunk(new Uint8Array(bytes), this.sequence++);
        }

        if (msg.isFinal === true) {
          options.onEnd();
          this.stopResolve?.();
          this.stopResolve = null;
          ws.close();
        }
      });

      ws.once('close', () => {
        if (!this.started) {
          // Closed before any audio was received — treat as end.
          options.onEnd();
        }
        this.stopResolve?.();
        this.stopResolve = null;
        this.ws = null;
      });
    });
  }

  sendText(text: string): void {
    if (!this.wsOpen) {
      // WS not open yet — buffer until `open` fires.
      this.preOpenQueue.push(text);
      return;
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    // Append a trailing space to improve prosody.
    const msg = JSON.stringify({ text: `${text} `, try_trigger_generation: true });
    this.ws.send(msg);
  }

  flush(): void {
    if (!this.wsOpen || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      // Defer flush — preOpenQueue will be replayed on open and the EOS in stop()
      // will close the stream after.
      return;
    }
    const msg = JSON.stringify({ text: '', flush: true });
    this.ws.send(msg);
  }

  async stop(): Promise<void> {
    // If WS hasn't opened yet, wait briefly for it (so the buffered text
    // actually gets sent before we EOS).
    if (!this.wsOpen && this.ws) {
      await new Promise<void>((res) => {
        const start = Date.now();
        const tick = (): void => {
          if (this.wsOpen || Date.now() - start > 3000) return res();
          setTimeout(tick, 25);
        };
        tick();
      });
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.resolve();
    }
    const ws = this.ws;
    return new Promise<void>((resolve) => {
      this.stopResolve = resolve;
      // Send empty text to signal end-of-stream.
      const msg = JSON.stringify({ text: '' });
      ws.send(msg);
      // Safety timeout: resolve after 5 s if server never closes.
      const timeout = setTimeout(() => {
        this.ws?.close();
        resolve();
      }, 5000);
      // Prevent the timeout from blocking process exit.
      timeout.unref?.();
    });
  }
}

/** Returns a new TtsAdapter instance. Each call creates a fresh instance (per-turn). */
export function createElevenLabsTtsAdapter(): TtsAdapter {
  return new ElevenLabsTtsAdapter();
}
