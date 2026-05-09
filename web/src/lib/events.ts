import { useEffect, useState } from 'react';

import { apiUrl } from './api';
import type { AgentState, SessionEvent } from '@/types/api';

// Live transcript + state via SSE. EventSource (not fetch) so the
// no-restricted-syntax fetch rule doesn't apply here.
//
// Behavior: maintains the last `state` and an append-only stream of
// finalized text blocks plus the current in-flight delta buffer.
// `text.final` flushes the buffer into a new transcript entry.

export interface LiveTranscript {
  state: AgentState;
  /** Finalized assistant turns, oldest first. */
  finals: string[];
  /** Currently streaming assistant text — empty between turns. */
  pending: string;
  /** Live provider errors, oldest first. */
  errors: string[];
  /** Wire-level connection state. */
  connection: 'connecting' | 'open' | 'closed';
}

const initial: LiveTranscript = {
  state: 'idle',
  finals: [],
  pending: '',
  errors: [],
  connection: 'connecting',
};

export function useSessionEvents(sessionId: string | undefined): LiveTranscript {
  const [live, setLive] = useState<LiveTranscript>(initial);

  useEffect(() => {
    if (!sessionId) return;
    setLive(initial);

    const es = new EventSource(apiUrl(`/sessions/${sessionId}/events`));

    es.onopen = () => {
      setLive((prev) => ({ ...prev, connection: 'open' }));
    };

    es.onmessage = (msg) => {
      let evt: SessionEvent;
      try {
        evt = JSON.parse(msg.data) as SessionEvent;
      } catch {
        // Malformed payload — ignore. The keep-alive comments don't
        // arrive here (EventSource swallows comment frames).
        return;
      }
      setLive((prev) => {
        switch (evt.type) {
          case 'state':
            return { ...prev, state: evt.state };
          case 'text.delta':
            return { ...prev, pending: prev.pending + evt.text };
          case 'text.final':
            return { ...prev, finals: [...prev.finals, evt.text], pending: '' };
          case 'error':
            return { ...prev, errors: [...prev.errors, evt.message], pending: '' };
        }
      });
    };

    es.onerror = () => {
      // EventSource auto-reconnects unless we close it. Surface the
      // gap to the UI; the browser will retry transparently.
      setLive((prev) => ({ ...prev, connection: 'connecting' }));
    };

    return () => {
      es.close();
      setLive((prev) => ({ ...prev, connection: 'closed' }));
    };
  }, [sessionId]);

  return live;
}
