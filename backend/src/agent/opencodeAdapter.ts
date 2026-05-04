import type {
  EventMessagePartUpdated,
  EventMessageUpdated,
  EventSessionIdle,
  EventSessionStatus,
  ToolPart,
} from '@opencode-ai/sdk/v2';
import { createOpencodeClient } from '@opencode-ai/sdk/v2/client';
import { nanoid } from 'nanoid';

import { config } from '../config.js';
import { logger } from '../logger.js';
import type { AgentAdapter } from './types.js';

// v2 SDK has EventMessagePartDelta which is the actual streaming token event.
// We redeclare it here because the v2 re-export doesn't include it at the top level.
interface EventMessagePartDelta {
  type: 'message.part.delta';
  properties: {
    sessionID: string;
    messageID: string;
    partID: string;
    field: string;
    delta: string;
  };
}

// Single client instance, shared across all sessions.
const client = createOpencodeClient({ baseUrl: config.opencodeBaseUrl });

type SubscribeHandlers = Parameters<AgentAdapter['subscribe']>[1];

// Per-session subscriber registry. Multiple calls to subscribe() for the same
// session append handlers to the same list; the global SSE loop fans out.
const sessionHandlers = new Map<string, SubscribeHandlers[]>();

// Track accumulated text per (sessionID, messageID) for onTextFinal.
// key = `${sessionID}:${messageID}`
const accumulatedDeltas = new Map<string, string>();

// Track completed assistant messageIDs so we can fire onTextFinal once.
const completedAssistantMessages = new Set<string>(); // `${sessionID}:${messageID}`

// Global SSE subscription — started on first subscribe() call and self-heals.
let sseStarted = false;
let sseReconnectAttempts = 0;
let lastSseEventAt = 0;
let sseGeneration = 0; // increments on each (re)connect; lets stale loops bail

// Reference to the currently-active stream so we can abort it from outside
// (watchdog / new-subscribe staleness check). AsyncGenerator.return() makes
// the in-flight `for await` exit immediately.
type SseStreamLike = { return?: (val?: unknown) => Promise<unknown> } | null;
let activeSseStream: SseStreamLike = null;

// Watchdog: if we haven't seen any SSE event for this long while at least one
// session is subscribed, assume the stream is half-closed and force reconnect.
const SSE_IDLE_RECONNECT_MS = 90_000;
let watchdogTimer: NodeJS.Timeout | null = null;

async function ensureSseStarted(): Promise<void> {
  if (sseStarted) return;
  sseStarted = true;
  sseReconnectAttempts = 0;
  lastSseEventAt = Date.now();
  startWatchdog();
  void runSseLoop();
}

function startWatchdog(): void {
  if (watchdogTimer) return;
  watchdogTimer = setInterval(() => {
    if (sessionHandlers.size === 0) return; // nobody listening — don't bother
    const idle = Date.now() - lastSseEventAt;
    if (idle > SSE_IDLE_RECONNECT_MS) {
      logger.warn('SSE watchdog: stream idle, forcing reconnect', { idleMs: idle });
      forceReconnect();
    }
  }, 15_000);
}

function forceReconnect(): void {
  // Bump the generation so the in-flight loop bails after its current iteration,
  // AND abort the underlying stream so a stalled `for await` actually exits.
  sseGeneration += 1;
  lastSseEventAt = Date.now();
  const stream = activeSseStream;
  activeSseStream = null;
  if (stream?.return) {
    stream.return().catch((err: unknown) => {
      logger.warn('forceReconnect: stream.return() failed', { err: String(err) });
    });
  }
}

async function runSseLoop(): Promise<void> {
  const myGen = ++sseGeneration;
  try {
    const result = await client.event.subscribe();
    if (myGen !== sseGeneration) {
      // Already superseded — close immediately and bail.
      const s = result.stream as unknown as SseStreamLike;
      if (s?.return) await s.return().catch(() => undefined);
      return;
    }
    activeSseStream = result.stream as unknown as SseStreamLike;
    logger.info('SSE stream connected', { attempt: sseReconnectAttempts, gen: myGen });
    sseReconnectAttempts = 0;
    lastSseEventAt = Date.now();
    for await (const rawEvent of result.stream) {
      if (myGen !== sseGeneration) {
        logger.info('SSE loop superseded — exiting', { gen: myGen });
        return;
      }
      lastSseEventAt = Date.now();
      const event = rawEvent as { type: string; properties?: unknown };
      handleSseEvent(event);
    }
    logger.warn('SSE stream ended cleanly — reconnecting', { gen: myGen });
  } catch (err) {
    logger.error('SSE stream error — reconnecting', { err: String(err), gen: myGen });
  } finally {
    if (
      activeSseStream !== null &&
      // Best-effort: only clear if this loop's stream is still the active one.
      myGen === sseGeneration - 0
    ) {
      activeSseStream = null;
    }
  }
  if (myGen !== sseGeneration) return; // already replaced
  // Reconnect with backoff, capped at 5s.
  const delay = Math.min(5000, 250 * Math.pow(2, sseReconnectAttempts));
  sseReconnectAttempts += 1;
  setTimeout(() => {
    void runSseLoop();
  }, delay);
}

function handleSseEvent(event: { type: string; properties?: unknown }): void {
  logger.debug('SSE event', { type: event.type });
  switch (event.type) {
    case 'message.part.delta': {
      // Streaming text token from the LLM. These fire only for assistant output
      // — user messages never produce part.delta events.
      const e = event as EventMessagePartDelta;
      const { sessionID, messageID, field, delta } = e.properties;
      if (field !== 'text') break;

      // Accumulate for onTextFinal.
      const key = `${sessionID}:${messageID}`;
      accumulatedDeltas.set(key, (accumulatedDeltas.get(key) ?? '') + delta);

      const handlers = sessionHandlers.get(sessionID);
      if (handlers) {
        for (const h of handlers) {
          h.onTextDelta(delta, messageID);
        }
      }
      break;
    }

    case 'message.updated': {
      // Fire onTextFinal when the assistant message has a completed time.end.
      const e = event as EventMessageUpdated;
      const msg = e.properties.info;
      if (msg.role !== 'assistant') break;

      // The SDK Message type uses `time` but the concrete field depends on the
      // discriminated union. We cast to reach the time field safely.
      const msgAny = msg as { id: string; sessionID: string; time?: { end?: number } };
      if (!msgAny.time?.end) break; // not yet complete

      const key = `${msg.sessionID}:${msgAny.id}`;
      if (completedAssistantMessages.has(key)) break; // already fired
      completedAssistantMessages.add(key);

      const accumulated = accumulatedDeltas.get(key) ?? '';
      accumulatedDeltas.delete(key);

      const handlers = sessionHandlers.get(msg.sessionID);
      if (handlers) {
        for (const h of handlers) {
          if (accumulated.length > 0 && h.onTextFinal) {
            h.onTextFinal(accumulated, msgAny.id);
          }
          // Per-turn completion signal — sessionManager uses this to mark the
          // turn done and dispatch the next queued turn. Without it, the state
          // stays 'running' across turns and every follow-up gets stuck in the
          // queue.
          if (h.onState) {
            h.onState('done');
          }
        }
      }
      break;
    }

    case 'message.part.updated': {
      const e = event as EventMessagePartUpdated;
      const { part } = e.properties;
      // Only handle tool parts — text is handled via message.part.delta.
      if (part.type !== 'tool') break;
      const toolPart = part as ToolPart;
      const handlers = sessionHandlers.get(toolPart.sessionID);
      if (handlers) {
        const phase = mapToolPhase(toolPart.state.status);
        for (const h of handlers) {
          if (h.onToolEvent) {
            const toolMsg = getToolMessage(toolPart);
            h.onToolEvent({
              phase,
              toolName: toolPart.tool,
              ...(toolMsg !== undefined && { message: toolMsg }),
            });
          }
        }
      }
      break;
    }

    case 'session.status': {
      const e = event as EventSessionStatus;
      const { sessionID, status } = e.properties;
      const handlers = sessionHandlers.get(sessionID);
      if (handlers) {
        const state = status.type === 'busy' ? 'running' : 'idle';
        for (const h of handlers) {
          if (h.onState) {
            h.onState(state);
          }
        }
      }
      break;
    }

    case 'session.idle': {
      const e = event as EventSessionIdle;
      const { sessionID } = e.properties;
      const handlers = sessionHandlers.get(sessionID);
      if (handlers) {
        for (const h of handlers) {
          if (h.onState) {
            h.onState('done');
          }
        }
      }
      break;
    }

    default:
      logger.debug('Unknown SSE event type', { type: event.type });
      break;
  }
}

function mapToolPhase(status: ToolPart['state']['status']): 'start' | 'update' | 'end' {
  switch (status) {
    case 'pending':
      return 'start';
    case 'running':
      return 'update';
    case 'completed':
    case 'error':
      return 'end';
  }
}

function getToolMessage(part: ToolPart): string | undefined {
  const { state } = part;
  if (state.status === 'running' && state.title) return state.title;
  if (state.status === 'completed') return state.title;
  if (state.status === 'error') return state.error;
  return undefined;
}

export const opencodeAdapter: AgentAdapter = {
  async createSession(input) {
    const title = input?.title;
    const result = await client.session.create(
      title !== undefined ? { title } : {},
      { throwOnError: true },
    );
    const session = result.data;
    if (!session) throw new Error('session.create returned no data');
    logger.info('Session created', { sessionId: session.id });
    return { sessionId: session.id };
  },

  async resumeSession(sessionId) {
    // Validate the session exists; throws if not found.
    await client.session.get({ sessionID: sessionId }, { throwOnError: true });
    logger.info('Session resumed', { sessionId });
    return { sessionId };
  },

  async sendTurn(sessionId, text) {
    const turnId = nanoid();
    await client.session.promptAsync(
      {
        sessionID: sessionId,
        parts: [{ type: 'text', text }],
      },
      { throwOnError: true },
    );
    logger.info('Turn sent', { sessionId, turnId });
    return { turnId };
  },

  async cancelTurn(sessionId) {
    await client.session.abort({ sessionID: sessionId }, { throwOnError: true });
    logger.info('Turn cancelled', { sessionId });
  },

  async subscribe(sessionId, handlers) {
    await ensureSseStarted();
    // If we haven't seen any SSE traffic in a while, assume the stream is
    // stale and force a fresh subscription before binding new handlers.
    if (Date.now() - lastSseEventAt > 30_000) {
      logger.info('SSE stream looks stale on new subscribe — forcing reconnect');
      forceReconnect();
    }

    let list = sessionHandlers.get(sessionId);
    if (!list) {
      list = [];
      sessionHandlers.set(sessionId, list);
    }
    list.push(handlers);
    logger.info('Subscribed to session events', { sessionId });

    return async () => {
      const current = sessionHandlers.get(sessionId);
      if (current) {
        const idx = current.indexOf(handlers);
        if (idx !== -1) current.splice(idx, 1);
        if (current.length === 0) sessionHandlers.delete(sessionId);
      }
      logger.info('Unsubscribed from session events', { sessionId });
    };
  },
};
