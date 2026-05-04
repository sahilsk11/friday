// Process entrypoint — HTTP server + WebSocket upgrade.

import { createServer } from 'http';

import { config } from './config.js';
import { logger } from './logger.js';
import { createWsServer, handleUpgrade } from './wsServer.js';

const wss = createWsServer();

const httpServer = createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true }));
    return;
  }
  res.writeHead(404);
  res.end();
});

httpServer.on('upgrade', (req, socket, head) => {
  handleUpgrade(wss, req, socket, head);
});

httpServer.listen(config.port, () => {
  logger.info(`voice-gateway listening on http://127.0.0.1:${String(config.port)} (ws path: /ws)`);
});
