import type { WebSocket } from 'ws';

import type { AgentAdapter } from './agent/types.js';
import { defaultRuntimeConfig } from './config.js';
import { logger } from './logger.js';
import { shouldSpeak } from './pipelines/speakingPolicy.js';
import { createTtsChunker } from './pipelines/ttsChunker.js';
import type { ServerMessage } from './protocol.js';
import { createElevenLabsTtsAdapter } from './tts/elevenLabsTtsAdapter.js';
import type { TtsAdapter } from './tts/elevenLabsTtsAdapter.js';

export type SessionState = 'idle' | 'running' | 'speaking';

interface QueuedTurn {
  text: string;
  source: 'typed' | 'stt-final';
}

interface TtsChunker {
  push(text: string): void;
  flush(): void;
  dispose(): void;
}

interface SessionEntry {
  sessionId: string;
  state: SessionState;
  queue: QueuedTurn[];
  currentTurnId: string | null;
  accumulatedText: Map<string, string>; // turnId → accumulated text
  unsubscribe: (() => Promise<void>) | null;
  // TTS state
  ttsAdapter: TtsAdapter | null;
  ttsChunker: TtsChunker | null;
  ttsTurnId: string | null;
}

// Map from sessionId → session entry
const sessions = new Map<string, SessionEntry>();

function send(ws: WebSocket, msg: ServerMessage): void {
  if (ws.readyState === ws.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function getOrCreateEntry(sessionId: string): SessionEntry {
  const existing = sessions.get(sessionId);
  if (existing) return existing;
  const entry: SessionEntry = {
    sessionId,
    state: 'idle',
    queue: [],
    currentTurnId: null,
    accumulatedText: new Map(),
    unsubscribe: null,
    ttsAdapter: null,
    ttsChunker: null,
    ttsTurnId: null,
  };
  sessions.set(sessionId, entry);
  return entry;
}

function transitionState(entry: SessionEntry, next: SessionState, ws: WebSocket): void {
  if (entry.state === next) return;
  entry.state = next;
  const stateMap: Record<SessionState, ServerMessage['type']> = {
    idle: 'session.state',
    running: 'session.state',
    speaking: 'session.state',
  };
  // Keep TS happy — we always emit session.state.
  void stateMap;
  send(ws, { type: 'session.state', sessionId: entry.sessionId, state: next });
}

function teardownTts(entry: SessionEntry): void {
  entry.ttsChunker?.dispose();
  entry.ttsChunker = null;
  entry.ttsAdapter = null;
  entry.ttsTurnId = null;
}

async function drainQueue(
  entry: SessionEntry,
  adapter: AgentAdapter,
  ws: WebSocket,
): Promise<void> {
  const next = entry.queue.shift();
  if (!next) return;
  await dispatchTurn(entry, next, adapter, ws);
}

async function dispatchTurn(
  entry: SessionEntry,
  turn: QueuedTurn,
  adapter: AgentAdapter,
  ws: WebSocket,
): Promise<void> {
  transitionState(entry, 'running', ws);
  send(ws, {
    type: 'agent.status',
    sessionId: entry.sessionId,
    status: 'thinking',
  });

  const { turnId } = await adapter.sendTurn(entry.sessionId, turn.text);
  entry.currentTurnId = turnId;

  // Subscribe to events if not already subscribed.
  if (!entry.unsubscribe) {
    entry.unsubscribe = await adapter.subscribe(entry.sessionId, {
      onTextDelta(text, _msgId) {
        const tid = entry.currentTurnId ?? 'unknown';
        const prev = entry.accumulatedText.get(tid) ?? '';
        entry.accumulatedText.set(tid, prev + text);
        send(ws, { type: 'agent.text.delta', sessionId: entry.sessionId, turnId: tid, text });

        // TTS pipeline: lazily initialize adapter+chunker on first speakable delta.
        if (defaultRuntimeConfig.autoSpeak && shouldSpeak(text)) {
          if (!entry.ttsAdapter) {
            entry.ttsTurnId = tid;
            const ttsAdapter = createElevenLabsTtsAdapter();
            const ttsChunker = createTtsChunker({
              maxChars: defaultRuntimeConfig.chunking.maxChars,
              maxDelayMs: defaultRuntimeConfig.chunking.maxDelayMs,
              sentenceBoundary: defaultRuntimeConfig.chunking.sentenceBoundary,
              onChunk(chunk: string) {
                ttsAdapter.sendText(chunk);
              },
            });
            entry.ttsAdapter = ttsAdapter;
            entry.ttsChunker = ttsChunker;

            ttsAdapter
              .start({
                sessionId: entry.sessionId,
                turnId: tid,
                voiceId: defaultRuntimeConfig.ttsVoiceId,
                modelId: defaultRuntimeConfig.ttsModelId,
                onStart() {
                  send(ws, { type: 'tts.started', sessionId: entry.sessionId, turnId: tid });
                },
                onChunk(bytes: Uint8Array, seq: number) {
                  send(ws, {
                    type: 'tts.audio.chunk',
                    sessionId: entry.sessionId,
                    turnId: tid,
                    sequence: seq,
                    audioBase64: Buffer.from(bytes).toString('base64'),
                    mimeType: 'audio/mpeg',
                  });
                },
                onEnd() {
                  send(ws, { type: 'tts.ended', sessionId: entry.sessionId, turnId: tid });
                  teardownTts(entry);
                },
                onError(err: Error) {
                  logger.error('TTS error', { sessionId: entry.sessionId, err: err.message });
                  teardownTts(entry);
                },
              })
              .catch((err: unknown) => {
                logger.error('TTS start failed', { sessionId: entry.sessionId, err });
                teardownTts(entry);
              });
          }

          entry.ttsChunker?.push(text);
        }
      },
      onTextFinal(text, _msgId) {
        const tid = entry.currentTurnId ?? 'unknown';
        entry.accumulatedText.set(tid, text);
        send(ws, { type: 'agent.text.final', sessionId: entry.sessionId, turnId: tid, text });

        // Flush and stop TTS for this turn.
        if (entry.ttsChunker) {
          entry.ttsChunker.flush();
        }
        if (entry.ttsAdapter) {
          void entry.ttsAdapter.stop().catch((err: unknown) => {
            logger.error('TTS stop failed on textFinal', { sessionId: entry.sessionId, err });
          });
        }
      },
      onToolEvent(event) {
        const tid = entry.currentTurnId;
        send(ws, {
          type: 'agent.tool',
          sessionId: entry.sessionId,
          ...(tid !== null && { turnId: tid }),
          phase: event.phase,
          toolName: event.toolName,
          ...(event.message !== undefined && { message: event.message }),
        });
        if (event.phase === 'start' || event.phase === 'update') {
          send(ws, {
            type: 'agent.status',
            sessionId: entry.sessionId,
            ...(tid !== null && { turnId: tid }),
            status: 'tool_running',
            ...(event.message !== undefined && { message: event.message }),
          });
        }
      },
      onState(state) {
        const tid = entry.currentTurnId;
        if (state === 'done' || state === 'idle') {
          // Flush + stop TTS if still active.
          if (entry.ttsChunker) {
            entry.ttsChunker.flush();
          }
          if (entry.ttsAdapter) {
            void entry.ttsAdapter.stop().catch((err: unknown) => {
              logger.error('TTS stop failed on state done', { sessionId: entry.sessionId, err });
            });
          }

          send(ws, {
            type: 'agent.status',
            sessionId: entry.sessionId,
            ...(tid !== null && { turnId: tid }),
            status: 'done',
          });
          transitionState(entry, 'idle', ws);
          entry.currentTurnId = null;
          // Process next queued turn.
          void drainQueue(entry, adapter, ws);
        } else if (state === 'running') {
          send(ws, {
            type: 'agent.status',
            sessionId: entry.sessionId,
            ...(tid !== null && { turnId: tid }),
            status: 'thinking',
          });
        }
      },
      onError(err) {
        logger.error('Agent error', { sessionId: entry.sessionId, err: err.message });
        teardownTts(entry);
        send(ws, {
          type: 'error',
          sessionId: entry.sessionId,
          code: 'agent_error',
          message: err.message,
          retryable: true,
        });
        transitionState(entry, 'idle', ws);
        entry.currentTurnId = null;
      },
    });
  }
}

export const sessionManager = {
  initSession(sessionId: string): void {
    getOrCreateEntry(sessionId);
  },

  async enqueueTurn(
    sessionId: string,
    text: string,
    source: 'typed' | 'stt-final',
    adapter: AgentAdapter,
    ws: WebSocket,
  ): Promise<{ turnId: string; queued: boolean }> {
    const entry = getOrCreateEntry(sessionId);

    if (entry.state === 'idle') {
      // Start immediately; but we need to return a turnId before it's assigned
      // by the adapter. We return the turnId after dispatchTurn sets it.
      await dispatchTurn(entry, { text, source }, adapter, ws);
      const turnId = entry.currentTurnId ?? 'unknown';
      return { turnId, queued: false };
    } else {
      entry.queue.push({ text, source });
      logger.info('Turn queued', { sessionId, queueLength: entry.queue.length });
      const turnId = `queued-${Date.now()}`;
      return { turnId, queued: true };
    }
  },

  async cancelTurn(sessionId: string, adapter: AgentAdapter, ws: WebSocket): Promise<void> {
    const entry = sessions.get(sessionId);
    if (!entry) return;

    // Stop TTS if active.
    teardownTts(entry);

    await adapter.cancelTurn(sessionId);
    transitionState(entry, 'idle', ws);
    entry.currentTurnId = null;
    // Clear the queue too
    entry.queue.length = 0;
  },

  stopTts(sessionId: string): void {
    const entry = sessions.get(sessionId);
    if (!entry) return;
    if (entry.ttsChunker) {
      entry.ttsChunker.flush();
    }
    if (entry.ttsAdapter) {
      void entry.ttsAdapter.stop().catch((err: unknown) => {
        logger.error('TTS stop failed on stopTts', { sessionId, err });
      });
    }
    teardownTts(entry);
  },

  async cleanup(sessionId: string): Promise<void> {
    const entry = sessions.get(sessionId);
    if (!entry) return;
    teardownTts(entry);
    if (entry.unsubscribe) {
      await entry.unsubscribe();
    }
    sessions.delete(sessionId);
  },

  getState(sessionId: string): SessionState | undefined {
    return sessions.get(sessionId)?.state;
  },
};
