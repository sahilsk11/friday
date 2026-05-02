import WebSocket from 'ws';

const ws = new WebSocket('ws://localhost:3000');
let sessionId = null;

ws.on('open', () => {
  console.log('Connected, creating session...');
  ws.send(JSON.stringify({ type: 'session.create', title: 'Test' }));
});

ws.on('message', (data) => {
  const msg = JSON.parse(data.toString());
  console.log('Received:', msg.type);

  if (msg.type === 'session.created' && !sessionId) {
    sessionId = msg.sessionId;
    console.log('Session ID:', sessionId);
    console.log('Sending turn...');
    ws.send(JSON.stringify({
      type: 'turn.send',
      sessionId,
      text: 'Hello',
      source: 'typed'
    }));
  }

  if (msg.type === 'turn.accepted') {
    console.log('Turn accepted, queued:', msg.queued);
  }

  if (msg.type === 'agent.text.delta') {
    console.log('Agent delta:', msg.text.substring(0, 80));
  }

  if (msg.type === 'agent.text.final') {
    console.log('Agent final:', msg.text.substring(0, 100));
  }

  if (msg.type === 'session.state') {
    console.log('Session state:', msg.state);
  }

  if (msg.type === 'agent.status') {
    console.log('Agent status:', msg.status, msg.message);
  }
});

ws.on('close', () => {
  console.log('Done');
  process.exit(0);
});

ws.on('error', (err) => {
  console.error('Error:', err.message);
  process.exit(1);
});

setTimeout(() => {
  console.log('Timeout - closing');
  ws.close();
  process.exit(0);
}, 45000);