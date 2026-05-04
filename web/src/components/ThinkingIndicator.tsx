import { RTVIEvent } from '@pipecat-ai/client-js';
import { useRTVIClientEvent } from '@pipecat-ai/client-react';
import { Thinking } from '@pipecat-ai/voice-ui-kit';
import { useCallback, useState } from 'react';

// "Thinking…" indicator tied to opencode's session state.
//
// Subscribes to the `agent-state` RTVI custom message that
// OpencodeProcessor publishes on every `session.status` / `session.idle`
// / `MessageUpdated(time_end)` event. Renders nothing while idle; pulses
// dim "thinking" + animated dots while busy.
//
// Why this — and why not a "loader" that just runs from prompt to first
// token: the indicator is gated on opencode's own state, so if the SSE
// stream dies or opencode is stuck, the pill goes idle (no event) and
// stops lying. The probe at server/scripts/probe_thinking_signals.py
// confirmed busy fires within ~20ms of the prompt and idle fires
// reliably at message complete. See PR description for measured numbers.
//
// We borrow the kit's <Thinking> dot animation rather than rolling our
// own — it's already exported from @pipecat-ai/voice-ui-kit, which we
// already depend on.

interface ServerMessageData {
  type: 'agent-state';
  state: string;
}

function isAgentStateMessage(value: unknown): value is ServerMessageData {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as { type?: unknown }).type === 'agent-state' &&
    typeof (value as { state?: unknown }).state === 'string'
  );
}

export function ThinkingIndicator(): React.ReactElement | null {
  const [thinking, setThinking] = useState(false);

  const onServerMessage = useCallback((raw: unknown) => {
    // Pipecat wraps our pushed dict in `{ data: <dict> }` when it
    // serializes RTVIServerMessageFrame — same unwrap as ActivityFeed.
    const inner: unknown = (raw as { data?: unknown } | null)?.data ?? raw;
    if (!isAgentStateMessage(inner)) return;
    setThinking(inner.state === 'thinking');
  }, []);

  useRTVIClientEvent(RTVIEvent.ServerMessage, onServerMessage);

  if (!thinking) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center justify-center gap-1 text-xs text-neutral-500"
    >
      <span className="animate-pulse">thinking</span>
      <Thinking className="font-mono" />
    </div>
  );
}
