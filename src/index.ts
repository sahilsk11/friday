import http from 'http';
import fs from 'fs';
import path from 'path';

import { config } from './config.js';
import { logger } from './logger.js';
import { SessionManager } from './sessionManager.js';
import type { RuntimeConfig, ServerMessage } from './types.js';

const frontentDist = path.join(new URL('.', import.meta.url).pathname, '../frontend/dist');

function serveStatic(req: http.IncomingMessage, res: http.ServerResponse): boolean {
  const urlPath = (req.url || '/').split('?')[0];
  const filePath = path.join(frontentDist, urlPath === '/' ? 'index.html' : urlPath);
  const resolved = path.resolve(filePath);
  if (!resolved.startsWith(path.resolve(frontentDist))) return false;
  if (!fs.existsSync(resolved) || fs.statSync(resolved).isDirectory()) return false;
  const ext = path.extname(resolved);
  const ct = ext === '.html' ? 'text/html' : ext === '.js' ? 'text/javascript' : ext === '.css' ? 'text/css' : 'application/octet-stream';
  res.writeHead(200, { 'Content-Type': ct });
  res.end(fs.readFileSync(resolved));
  return true;
}

const sessionManager = new SessionManager();

// One SSE writer per session. Replacing the old WebSocket clients map: messages
// from the agent/STT/TTS pipelines flow back to whichever response is currently
// holding the SSE stream open. If the client disconnects mid-turn, messages
// silently drop until they reopen — same behaviour as the old WS path.
const sseClients = new Map<string, http.ServerResponse>();

function sendSse(res: http.ServerResponse, msg: ServerMessage): void {
  res.write(`data: ${JSON.stringify(msg)}\n\n`);
}

sessionManager.on('message', (sessionId: string, message: ServerMessage) => {
  const res = sseClients.get(sessionId);
  if (res) sendSse(res, message);
});

async function readJson<T>(req: http.IncomingMessage): Promise<T> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  const body = Buffer.concat(chunks).toString('utf-8');
  return (body ? JSON.parse(body) : {}) as T;
}

async function readBuffer(req: http.IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  return Buffer.concat(chunks);
}

function jsonResp(res: http.ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && serveStatic(req, res)) return;
    const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
    const path = url.pathname;
    const method = req.method || 'GET';

    if (method === 'GET' && path === '/api/ping') {
      jsonResp(res, 200, { ok: true });
      return;
    }

    if (method === 'GET' && path === '/api/sessions') {
      const response = await fetch(`${config.opencodeUrl}/session`);
      const sessions = (await response.json()) as Array<{ id: string; title: string; time: { created: number } }>;
      const list = sessions.map(s => ({
        id: s.id,
        title: s.title || 'Untitled',
        created: s.time?.created,
      }));
      jsonResp(res, 200, { sessions: list });
      return;
    }

    const adoptMatch = path.match(/^\/api\/session\/adopt\/(.+)$/);
    if (method === 'POST' && adoptMatch) {
      const opencodeSessionId = adoptMatch[1];
      const result = await sessionManager.adoptOpenCodeSession(opencodeSessionId);
      jsonResp(res, 200, { sessionId: result.sessionId });
      return;
    }

    const messagesMatch = path.match(/^\/api\/session\/messages\/(.+)$/);
    if (method === 'GET' && messagesMatch) {
      const opencodeSessionId = messagesMatch[1];
      const response = await fetch(`${config.opencodeUrl}/session/${opencodeSessionId}/message`);
      const messages = (await response.json()) as Array<{ info: { role: string; id: string }; parts: { type: string; text: string }[] }>;
      const list = messages.map(m => ({
        role: m.info?.role,
        id: m.info?.id,
        content: m.parts?.find(p => p.type === 'text')?.text || '',
      }));
      jsonResp(res, 200, { messages: list });
      return;
    }

    if (method === 'POST' && path === '/api/session') {
      const body = await readJson<{ title?: string }>(req);
      const result = await sessionManager.createSession(body.title);
      jsonResp(res, 200, { sessionId: result.sessionId, title: body.title });
      return;
    }

    const eventsMatch = path.match(/^\/api\/session\/([^/]+)\/events$/);
    if (method === 'GET' && eventsMatch) {
      const sessionId = eventsMatch[1];
      res.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache, no-transform',
        connection: 'keep-alive',
        // Disable proxy buffering. nginx and a few CF edge paths buffer
        // text/event-stream by default, which delays deltas to the browser.
        'x-accel-buffering': 'no',
      });
      res.write(': ok\n\n');
      sseClients.set(sessionId, res);
      logger.info('SSE client connected', { sessionId });

      const heartbeat = setInterval(() => res.write(': hb\n\n'), 15000);

      const close = (): void => {
        clearInterval(heartbeat);
        if (sseClients.get(sessionId) === res) {
          sseClients.delete(sessionId);
          sessionManager.disconnectClient(sessionId);
          logger.info('SSE client disconnected', { sessionId });
        }
      };
      req.on('close', close);
      req.on('aborted', close);
      return;
    }

    const sessMatch = path.match(/^\/api\/session\/([^/]+)\/(.+)$/);
    if (method === 'POST' && sessMatch) {
      const sessionId = sessMatch[1];
      const action = sessMatch[2];

      switch (action) {
        case 'resume': {
          await sessionManager.resumeSession(sessionId);
          jsonResp(res, 200, { ok: true });
          return;
        }
        case 'audio/start': {
          const body = await readJson<{
            sampleRate: number;
            encoding: 'pcm16';
            language?: string;
            sttProvider?: 'elevenlabs';
          }>(req);
          await sessionManager.startStt(sessionId, {
            type: 'audio.start',
            sessionId,
            ...body,
          });
          jsonResp(res, 200, { ok: true });
          return;
        }
        case 'audio/chunk': {
          // Raw PCM16 in the body — no base64 wrapper, since there's no JSON
          // envelope. SessionManager still wants base64, so encode there.
          const buf = await readBuffer(req);
          await sessionManager.sendSttAudio(sessionId, {
            type: 'audio.chunk',
            sessionId,
            chunkBase64: buf.toString('base64'),
            sequence: Date.now(),
          });
          res.writeHead(204);
          res.end();
          return;
        }
        case 'audio/stop': {
          await sessionManager.stopStt(sessionId);
          jsonResp(res, 200, { ok: true });
          return;
        }
        case 'turn': {
          const body = await readJson<{ text: string; source: 'typed' | 'stt-final' }>(req);
          await sessionManager.sendTurn(sessionId, body.text, body.source);
          jsonResp(res, 200, { ok: true });
          return;
        }
        case 'cancel': {
          const body = await readJson<{ turnId?: string }>(req);
          await sessionManager.cancelRun(sessionId, body.turnId);
          jsonResp(res, 200, { ok: true });
          return;
        }
        case 'tts/stop': {
          await sessionManager.stopTts(sessionId);
          jsonResp(res, 200, { ok: true });
          return;
        }
        case 'config': {
          const body = await readJson<{ config: Partial<RuntimeConfig> }>(req);
          sessionManager.updateConfig(sessionId, body.config);
          jsonResp(res, 200, { ok: true });
          return;
        }
      }
    }

    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
  } catch (err) {
    logger.error('Request error', { error: String(err) });
    if (!res.headersSent) jsonResp(res, 500, { error: String(err) });
  }
});

server.listen(config.port, () => {
  logger.info(`Voice Gateway listening on port ${config.port}`);
});
