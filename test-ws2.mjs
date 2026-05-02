import WebSocket from 'ws';

console.log('Connecting to ws://localhost:3000...');
const ws = new WebSocket('ws://localhost:3000');

ws.on('open', () => {
  console.log('Connected!');
  ws.send(JSON.stringify({ type: 'session.create', title: 'Test' }));
});

ws.on('message', (data) => {
  const msg = JSON.parse(data.toString());
  console.log('Message:', JSON.stringify(msg, null, 2));
  if (msg.type === 'error') {
    console.log('Error:', msg.message);
  }
});

ws.on('close', () => {
  console.log('Disconnected');
  process.exit(0);
});

ws.on('error', (err) => {
  console.error('WS Error:', err.message);
  process.exit(1);
});

setTimeout(() => {
  console.log('No messages received after 5s');
  ws.close();
  process.exit(1);
}, 5000);