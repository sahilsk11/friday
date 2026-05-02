import WebSocket from 'ws';

const ws = new WebSocket('ws://localhost:3000');

ws.on('open', () => {
  console.log('Connected to WebSocket');
  ws.send(JSON.stringify({ type: 'session.create', title: 'Test Session' }));
});

ws.on('message', (data) => {
  const msg = JSON.parse(data.toString());
  console.log('Received:', msg.type);
  if (msg.type === 'session.created') {
    console.log('Session ID:', msg.sessionId);
    ws.send(JSON.stringify({ type: 'turn.send', sessionId: msg.sessionId, text: 'Hello', source: 'typed' }));
  }
  if (msg.type === 'turn.accepted') {
    console.log('Turn accepted, queued:', msg.queued);
    ws.close();
  }
});

ws.on('error', (err) => {
  console.error('Error:', err.message);
});

ws.on('close', () => {
  console.log('Connection closed');
  process.exit(0);
});

setTimeout(() => {
  console.log('Timeout - closing');
  ws.close();
  process.exit(1);
}, 5000);