import { createOpencodeClient } from '@opencode-ai/sdk/v2/client';

const client = createOpencodeClient({ baseUrl: 'http://127.0.0.1:4096' });

console.log('Subscribing to SSE...');
const result = await client.event.subscribe();
console.log('Got result:', JSON.stringify(Object.keys(result)));
console.log('Stream type:', typeof result.stream);

// Create a session and send a prompt so events fire
const sess = await client.session.create({ title: 'debug-test' }, { throwOnError: true });
const sessionID = sess.data.id;
console.log('Created session:', sessionID);

void client.session.promptAsync({ sessionID, parts: [{ type: 'text', text: 'say hi' }] }, { throwOnError: true });

console.log('Starting stream iteration (10s timeout)...');
const timeout = setTimeout(() => { console.log('Timeout'); process.exit(0); }, 10000);

let count = 0;
for await (const event of result.stream) {
  count++;
  const e = event;
  console.log('EVENT:', JSON.stringify(e).slice(0, 400));
  if (count >= 20) break;
}
clearTimeout(timeout);
console.log('Done, events:', count);
