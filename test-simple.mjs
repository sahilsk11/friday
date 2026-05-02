import WebSocket from 'ws';

const ws = new WebSocket('ws://localhost:3000', { handshakeTimeout: 5000 });

ws.on('open', () => {
  console.log('Connected');
  ws.send(JSON.stringify({ type: 'session.create' }));
});

ws.on('message', (data) => {
  console.log('Received:', data.toString().substring(0, 100));
});

ws.on('error', (e) => {
  console.error('Error:', e.message);
});

ws.on('close', () => {
  console.log('Closed');
  process.exit(0);
});

setTimeout(() => { ws.close(); process.exit(1); }, 10000);