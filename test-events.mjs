import { EventSource } from 'eventsource';

console.log('Connecting to http://127.0.0.1:4096/event...');
const es = new EventSource('http://127.0.0.1:4096/event');

es.onopen = () => {
  console.log('Connected to event stream!');
};

es.onmessage = (event) => {
  console.log('Event:', event.data);
};

es.onerror = (err) => {
  console.error('Error:', err);
  es.close();
};

setTimeout(() => {
  console.log('Closing...');
  es.close();
  process.exit(0);
}, 5000);