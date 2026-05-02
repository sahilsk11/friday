import { EventSource } from 'eventsource';

const es = new EventSource('http://127.0.0.1:4097/event');

es.onmessage = (event) => {
  console.log('RAW EVENT:', event.data);
};

es.onerror = (err) => {
  console.error('Error:', err);
};

setTimeout(() => {
  es.close();
  process.exit(0);
}, 10000);