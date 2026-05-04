import type { IncomingMessage } from 'http';
import type { Duplex } from 'stream';
import type { WebSocket } from 'ws';
import { WebSocketServer } from 'ws';

import { opencodeAdapter } from './agent/opencodeAdapter.js';
import { logger } from './logger.js';
import type { ClientMessage, ServerMessage } from './protocol.js';
import { sessionManager } from './sessionManager.js';
import { createElevenLabsSttAdapter } from './stt/elevenLabsSttAdapter.js';
import type { SttAdapter } from './stt/elevenLabsSttAdapter.js';

// Per-connection STT adapter map: sessionId → adapter
const sttAdapters = new Map<string, SttAdapter>();

function send(ws: WebSocket, msg: ServerMessage): void {
  if (ws.readyState === ws.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function sendError(
  ws: WebSocket,
  code: string,
  message: string,
  sessionId?: string,
  retryable = false,
): void {
  send(ws, {
    type: 'error',
    code,
    message,
    ...(sessionId !== undefined && { sessionId }),
    ...(retryable && { retryable }),
  });
}

function parseClientMessage(raw: string): ClientMessage | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null || !('type' in parsed)) {
      return null;
    }
    return parsed as ClientMessage;
  } catch {
    return null;
  }
}

async function handleMessage(ws: WebSocket, raw: string): Promise<void> {
  const msg = parseClientMessage(raw);
  if (!msg) {
    sendError(ws, 'invalid_message', 'Could not parse message as JSON with a type field.');
    return;
  }

  switch (msg.type) {
    case 'ping':
      send(ws, { type: 'pong', ts: msg.ts });
      return;

    case 'session.create': {
      try {
        const title = msg.title;
        const { sessionId } = await opencodeAdapter.createSession(
          title !== undefined ? { title } : undefined,
        );
        sessionManager.initSession(sessionId);
        send(ws, {
          type: 'session.created',
          sessionId,
          ...(title !== undefined && { title }),
        });
      } catch (err) {
        logger.error('session.create error', err);
        sendError(ws, 'session_create_failed', String(err), undefined, true);
      }
      return;
    }

    case 'session.resume': {
      try {
        const { sessionId } = await opencodeAdapter.resumeSession(msg.sessionId);
        sessionManager.initSession(sessionId);
        send(ws, { type: 'session.resumed', sessionId });
      } catch (err) {
        logger.error('session.resume error', err);
        sendError(ws, 'session_resume_failed', String(err), msg.sessionId, false);
      }
      return;
    }

    case 'turn.send': {
      const { sessionId, text, source } = msg;
      try {
        const { turnId, queued } = await sessionManager.enqueueTurn(
          sessionId,
          text,
          source,
          opencodeAdapter,
          ws,
        );
        send(ws, { type: 'turn.accepted', sessionId, turnId, queued });
      } catch (err) {
        logger.error('turn.send error', err);
        sendError(ws, 'turn_send_failed', String(err), sessionId, true);
      }
      return;
    }

    case 'run.cancel': {
      const { sessionId } = msg;
      try {
        await sessionManager.cancelTurn(sessionId, opencodeAdapter, ws);
        const cancelTurnId = msg.turnId;
        send(ws, {
          type: 'run.cancelled',
          sessionId,
          ...(cancelTurnId !== undefined && { turnId: cancelTurnId }),
        });
      } catch (err) {
        logger.error('run.cancel error', err);
        sendError(ws, 'cancel_failed', String(err), sessionId, true);
      }
      return;
    }

    case 'audio.start': {
      const { sessionId, language, sampleRate } = msg;

      // Clean up any lingering adapter for this session
      const existing = sttAdapters.get(sessionId);
      if (existing) {
        existing.stop().catch((err: unknown) => {
          logger.warn('audio.start: error stopping previous adapter', err);
        });
        sttAdapters.delete(sessionId);
      }

      const adapter = createElevenLabsSttAdapter();

      try {
        await adapter.start({
          sessionId,
          language,
          sampleRate,
          onPartial(text: string) {
            send(ws, { type: 'stt.partial', sessionId, text });
          },
          onFinal(text: string) {
            send(ws, { type: 'stt.final', sessionId, text });
            // autoSendFinalTranscript: default true — enqueue turn immediately
            sessionManager
              .enqueueTurn(sessionId, text, 'stt-final', opencodeAdapter, ws)
              .then(({ turnId, queued }) => {
                send(ws, { type: 'turn.accepted', sessionId, turnId, queued });
              })
              .catch((err: unknown) => {
                logger.error('audio.start onFinal enqueueTurn error', err);
                sendError(ws, 'turn_send_failed', String(err), sessionId, true);
              });
          },
          onError(error: Error) {
            logger.error('STT error', { sessionId, err: error.message });
            send(ws, {
              type: 'error',
              sessionId,
              code: 'stt_error',
              message: error.message,
              retryable: true,
            });
            sttAdapters.delete(sessionId);
          },
        });

        sttAdapters.set(sessionId, adapter);

        // Emit listening state immediately; transcribing will be implied by first onPartial
        send(ws, { type: 'session.state', sessionId, state: 'listening' });
      } catch (err) {
        logger.error('audio.start: failed to connect STT', { sessionId, err });
        sendError(ws, 'stt_connect_failed', String(err), sessionId, true);
      }
      return;
    }

    case 'audio.chunk': {
      const { sessionId, chunkBase64 } = msg;
      const adapter = sttAdapters.get(sessionId);
      if (!adapter) {
        // Silently drop — emitting an error per chunk floods the UI when STT
        // fails mid-stream and the client hasn't stopped capture yet.
        logger.debug('audio.chunk dropped: no active STT adapter', { sessionId });
        return;
      }
      // Decode base64 → Uint8Array and forward to ElevenLabs
      const chunk = new Uint8Array(Buffer.from(chunkBase64, 'base64'));
      adapter.sendAudio(chunk);
      // On the first chunk, transition to transcribing state
      send(ws, { type: 'session.state', sessionId, state: 'transcribing' });
      return;
    }

    case 'audio.stop': {
      const { sessionId } = msg;
      const adapter = sttAdapters.get(sessionId);
      if (!adapter) {
        // No adapter is not an error — idempotent stop
        return;
      }
      sttAdapters.delete(sessionId);
      adapter.stop().catch((err: unknown) => {
        logger.error('audio.stop: error stopping STT adapter', { sessionId, err });
      });
      return;
    }

    case 'tts.stop':
      sessionManager.stopTts(msg.sessionId);
      return;

    case 'config.update':
      // Accept but ignore for now — Phase 2 will wire RuntimeConfig.
      logger.info('config.update received (ignored in Phase 1)', { config: msg.config });
      return;

    default: {
      // TypeScript exhaustiveness: the type system doesn't cover unknown future
      // fields, so we do a runtime fallback.
      const unknownType = (msg as { type: string }).type;
      sendError(ws, 'unknown_message_type', `Unknown message type: ${unknownType}`);
    }
  }
}

export function createWsServer(): WebSocketServer {
  const wss = new WebSocketServer({ noServer: true });

  wss.on('connection', (ws: WebSocket, req: IncomingMessage) => {
    logger.info('WS connection established', { url: req.url });

    ws.on('message', (data) => {
      const raw = data.toString();
      void handleMessage(ws, raw).catch((err: unknown) => {
        logger.error('Unhandled WS message error', err);
      });
    });

    ws.on('close', () => {
      logger.info('WS connection closed');
    });

    ws.on('error', (err) => {
      logger.error('WS connection error', err);
    });
  });

  return wss;
}

export function handleUpgrade(
  wss: WebSocketServer,
  req: IncomingMessage,
  socket: Duplex,
  head: Buffer,
): void {
  const url = req.url ?? '';
  if (url !== '/ws' && !url.startsWith('/ws?')) {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit('connection', ws, req);
  });
}
