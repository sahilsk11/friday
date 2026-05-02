import { EventEmitter } from 'events';
import { v4 as uuidv4 } from 'uuid';

import { OpenCodeAdapter } from './agent/opencodeAdapter.js';
import { config } from './config.js';
import { logger } from './logger.js';
import { SpeakingPolicy } from './pipelines/speakingPolicy.js';
import { ElevenLabsSttAdapter } from './stt/elevenLabsSttAdapter.js';
import type { SttAdapter } from './stt/types.js';
import { ElevenLabsTtsAdapter } from './tts/elevenLabsTtsAdapter.js';
import type { TtsAdapter } from './tts/types.js';
import { DEFAULT_CONFIG } from './types.js';
import type { ClientMessage, RuntimeConfig} from './types.js';

interface Session {
  id: string;
  state: 'idle' | 'listening' | 'transcribing' | 'running' | 'speaking' | 'error';
  config: RuntimeConfig;
  turnQueue: string[];
  currentTurnId?: string;
  sttAdapter?: SttAdapter;
  ttsAdapter?: TtsAdapter;
  // Tracks the in-flight queueTtsText promise. onState('done') awaits this
  // before flushing/closing TTS — otherwise the flush races with the WS open
  // and ElevenLabs gets "config + empty" with no actual text to speak.
  ttsQueue?: Promise<void>;
  opencodeSessionId?: string;
  agentAdapter: OpenCodeAdapter;
}

export class SessionManager extends EventEmitter {
  private sessions = new Map<string, Session>();
  private sttFactory: () => SttAdapter;
  private ttsFactory: () => TtsAdapter;
  private speakingPolicy: SpeakingPolicy;
  private defaultOpencodeUrl: string;

  constructor() {
    super();
    this.defaultOpencodeUrl = config.opencodeUrl;
    this.sttFactory = () =>
      new ElevenLabsSttAdapter(
        config.elevenlabsApiKey,
        (_text) => {
          // onPartial - handled via start options
        },
        (_text) => {
          // onFinal - handled via start options
        }
      );
    this.ttsFactory = () =>
      new ElevenLabsTtsAdapter(config.elevenlabsApiKey);
    this.speakingPolicy = new SpeakingPolicy();
  }

  async createSession(title?: string, opencodeUrl?: string): Promise<{ sessionId: string }> {
    const sessionId = uuidv4();
    const url = opencodeUrl || this.defaultOpencodeUrl;
    const agentAdapter = new OpenCodeAdapter(url);
    const agentSession = await agentAdapter.createSession({ title });

    const session: Session = {
      id: sessionId,
      state: 'idle',
      config: { ...DEFAULT_CONFIG },
      turnQueue: [],
      opencodeSessionId: agentSession.sessionId,
      agentAdapter,
    };

    this.sessions.set(sessionId, session);
    logger.info('Session created', { sessionId, opencodeSessionId: agentSession.sessionId });

    await this.subscribeToAgent(sessionId);

    return { sessionId };
  }

  async adoptOpenCodeSession(opencodeSessionId: string): Promise<{ sessionId: string }> {
    const agentAdapter = new OpenCodeAdapter(this.defaultOpencodeUrl);
    await agentAdapter.resumeSession(opencodeSessionId);

    const sessionId = uuidv4();
    const session: Session = {
      id: sessionId,
      state: 'idle',
      config: { ...DEFAULT_CONFIG },
      turnQueue: [],
      opencodeSessionId,
      agentAdapter,
    };

    this.sessions.set(sessionId, session);
    logger.info('Session adopted', { sessionId, opencodeSessionId });

    await this.subscribeToAgent(sessionId);

    return { sessionId };
  }

  async resumeSession(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new Error(`Session ${sessionId} not found`);
    }
    await this.subscribeToAgent(sessionId);
    logger.info('Session resumed', { sessionId });
  }

  private async subscribeToAgent(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session || !session.opencodeSessionId) return;

    await session.agentAdapter.subscribe(
      session.opencodeSessionId,
      {
        onTextDelta: (text, turnId) => {
          this.emit('message', sessionId, {
            type: 'agent.text.delta',
            sessionId,
            turnId: turnId || '',
            text,
          });
          if (session.config.autoSpeak) {
            const shouldSpeak = this.speakingPolicy.shouldSpeak(text);
            if (shouldSpeak) {
              const prev = session.ttsQueue ?? Promise.resolve();
              session.ttsQueue = prev.then(() =>
                this.queueTtsText(sessionId, turnId || '', text)
              );
            }
          }
        },
        onTextFinal: (text, turnId) => {
          this.emit('message', sessionId, {
            type: 'agent.text.final',
            sessionId,
            turnId: turnId || '',
            text,
          });
          // ElevenLabs streaming TTS doesn't emit audio until it sees the
          // empty-text terminator. The partial-delta path may also have
          // skipped queueing via shouldSpeak (short responses). So at final
          // we (a) ensure the full text is queued if nothing went earlier,
          // then (b) call stopTts which flushes + closes the WS — the close
          // event in the adapter triggers tts.ended down the SSE.
          const sess = this.sessions.get(sessionId);
          if (sess?.config.autoSpeak) {
            void (async () => {
              if (!sess.ttsAdapter) {
                await this.queueTtsText(sessionId, turnId || '', text);
              }
              if (sess.ttsAdapter) {
                await this.stopTts(sessionId);
              }
              this.updateSessionState(sessionId, 'idle');
            })();
          } else {
            this.updateSessionState(sessionId, 'idle');
          }
        },
        onToolEvent: (event) => {
          this.emit('message', sessionId, {
            type: 'agent.tool',
            sessionId,
            turnId: session.currentTurnId,
            phase: event.phase,
            toolName: event.toolName,
            message: event.message,
          });
        },
        onState: (state) => {
          if (state === 'running') {
            this.updateSessionState(sessionId, 'running');
          } else if (state === 'idle' || state === 'done') {
            // OpenCodeAdapter never fires onTextFinal — it signals completion
            // via onState('done'). Use this as the trigger to flush+close
            // TTS so ElevenLabs emits the buffered audio. Wait for any
            // in-flight queueTtsText so the actual text is sent before the
            // empty-text flush.
            void (async () => {
              const sess = this.sessions.get(sessionId);
              if (state === 'done') {
                if (sess?.ttsQueue) {
                  try {
                    await sess.ttsQueue;
                  } catch {
                    /* ignore */
                  }
                }
                if (sess?.ttsAdapter) {
                  await this.stopTts(sessionId);
                }
                if (sess) sess.ttsQueue = undefined;
              }
              await this.processQueue(sessionId);
            })();
          }
        },
        onError: (error) => {
          logger.error('Agent error', { sessionId, error: error.message });
          this.emit('message', sessionId, {
            type: 'error',
            sessionId,
            code: 'AGENT_ERROR',
            message: error.message,
          });
          this.updateSessionState(sessionId, 'error');
        },
      },
      sessionId
    );
  }

  async startStt(sessionId: string, options: ClientMessage & { type: 'audio.start' }): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) throw new Error(`Session ${sessionId} not found`);

    session.sttAdapter = this.sttFactory();
    await session.sttAdapter.start({
      sessionId,
      language: options.language,
      onPartial: (text) => {
        this.emit('message', sessionId, { type: 'stt.partial', sessionId, text });
      },
      onFinal: (text) => {
        this.emit('message', sessionId, { type: 'stt.final', sessionId, text });
        if (session.config.autoSendFinalTranscript) {
          void this.sendTurn(sessionId, text, 'stt-final');
        }
      },
      onError: (error) => {
        this.emit('message', sessionId, {
          type: 'error',
          sessionId,
          code: 'STT_ERROR',
          message: error.message,
        });
      },
    });
  }

  async sendSttAudio(
    sessionId: string,
    message: ClientMessage & { type: 'audio.chunk' }
  ): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session?.sttAdapter) return;

    const audioData = Buffer.from(message.chunkBase64, 'base64');
    session.sttAdapter.sendAudio(audioData.buffer.slice(audioData.byteOffset, audioData.byteOffset + audioData.byteLength));
  }

  async stopStt(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session?.sttAdapter) return;

    await session.sttAdapter.stop();
    session.sttAdapter = undefined;
  }

  async sendTurn(sessionId: string, text: string, _source: 'typed' | 'stt-final'): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) throw new Error(`Session ${sessionId} not found`);

    if (session.state === 'idle') {
      session.currentTurnId = uuidv4();
      this.updateSessionState(sessionId, 'running');

      this.emit('message', sessionId, {
        type: 'turn.accepted',
        sessionId,
        turnId: session.currentTurnId,
        queued: false,
        message: "Alright, I'm on it. Let me check that out.",
      });

      if (session.opencodeSessionId) {
        await session.agentAdapter.sendTurn(session.opencodeSessionId, text);
      }
    } else {
      session.turnQueue.push(text);
      this.emit('message', sessionId, {
        type: 'turn.accepted',
        sessionId,
        turnId: uuidv4(),
        queued: true,
        message: "Got it. I've added that to the queue.",
      });
    }
  }

  private async processQueue(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) return;

    const nextText = session.turnQueue.shift();
    if (nextText) {
      session.currentTurnId = uuidv4();
      this.updateSessionState(sessionId, 'running');

      this.emit('message', sessionId, {
        type: 'turn.accepted',
        sessionId,
        turnId: session.currentTurnId,
        queued: false,
      });

      if (session.opencodeSessionId) {
        await session.agentAdapter.sendTurn(session.opencodeSessionId, nextText);
      }
    } else {
      this.updateSessionState(sessionId, 'idle');
    }
  }

  async cancelRun(sessionId: string, turnId?: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session || !session.opencodeSessionId) return;

    await session.agentAdapter.cancelTurn(session.opencodeSessionId, turnId);
    session.turnQueue = [];

    this.emit('message', sessionId, {
      type: 'run.cancelled',
      sessionId,
      turnId: turnId || session.currentTurnId,
    });

    this.updateSessionState(sessionId, 'idle');
  }

  async stopTts(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session?.ttsAdapter) return;

    await session.ttsAdapter.stop();
    session.ttsAdapter = undefined;

    this.emit('message', sessionId, {
      type: 'tts.ended',
      sessionId,
      turnId: session.currentTurnId || '',
    });
  }

  updateConfig(sessionId: string, configUpdate: Partial<RuntimeConfig>): void {
    const session = this.sessions.get(sessionId);
    if (!session) return;

    session.config = { ...session.config, ...configUpdate };
  }

  disconnectClient(sessionId: string): void {
    logger.info('Client disconnected from session', { sessionId });
  }

  private updateSessionState(sessionId: string, state: Session['state']): void {
    const session = this.sessions.get(sessionId);
    if (!session) return;

    session.state = state;
    this.emit('message', sessionId, { type: 'session.state', sessionId, state });
  }

  private async queueTtsText(sessionId: string, turnId: string, text: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) return;

    // Capture a local handle. onState('done') can call stopTts() while we're
    // awaiting start() below, which clears session.ttsAdapter. Without this
    // local ref, the sendText at the end NPEs.
    let adapter = session.ttsAdapter;
    if (!adapter) {
      adapter = this.ttsFactory();
      session.ttsAdapter = adapter;
      await adapter.start({
        sessionId,
        turnId,
        voiceId: session.config.ttsVoiceId,
        modelId: session.config.ttsModelId,
        onChunk: (chunk) => {
          this.emit('message', sessionId, {
            type: 'tts.audio.chunk',
            sessionId,
            turnId,
            sequence: chunk.sequence,
            audioBase64: chunk.audioBase64,
            mimeType: chunk.mimeType,
          });
        },
        onStart: () => {
          this.emit('message', sessionId, { type: 'tts.started', sessionId, turnId });
          this.updateSessionState(sessionId, 'speaking');
        },
        onEnd: () => {
          this.emit('message', sessionId, { type: 'tts.ended', sessionId, turnId });
          this.updateSessionState(sessionId, 'idle');
        },
        onError: (error) => {
          this.emit('message', sessionId, {
            type: 'error',
            sessionId,
            code: 'TTS_ERROR',
            message: error.message,
          });
          this.updateSessionState(sessionId, 'idle');
        },
      });
    }

    // If stopTts ran while we were awaiting start(), the adapter we hold is
    // already closed and session.ttsAdapter is undefined. Skip the send.
    if (session.ttsAdapter === adapter) {
      adapter.sendText(text);
    }
  }
}