import { RTVIEvent } from '@pipecat-ai/client-js';
import { useRTVIClientEvent } from '@pipecat-ai/client-react';
import { useCallback, useMemo, useState } from 'react';

import type { TranscriptEntry } from '@/types/api';

// Chronological feed of user finals, assistant turns, and tool starts.
//
// Sources, all flowing on the voice-room WebSocket as RTVI events
// (see TRANSPORT.md):
//
//   - serverMessage (custom emissions from server):
//       From UserTranscriptMirror (pipecat_adapter.py):
//         { type: "user-transcript-running", text }   # ignored here, see RunningUserTranscript
//         { type: "user-transcript-final",   text }   # locks a user entry
//       From OpencodeProcessor (pipecat_adapter.py):
//         { type: "tool-started",        name }
//         { type: "assistant-text-delta", text }
//         { type: "assistant-text-final", text }
//         { type: "agent-state",         state }
//         { type: "assistant-error",     message }
//
// Why a custom user-transcript-final instead of pipecat's built-in
// userTranscript event: Friday locks user text into the feed only when
// Pipecat's user-turn aggregator says the whole turn is complete. The
// observer's built-in emit is disabled in server.py so STT segments do not
// become separate feed entries.
//
// We append deltas onto the trailing assistant entry as they stream so
// the UI feels alive while opencode generates. ``assistant-text-final``
// locks the entry. New tool starts and user turns push new entries.
//
// jarvis.md FE/BE rule: this lives on RTVI rather than the persisted
// SSE transcript stream. Per TRANSPORT.md we deliberately bend that
// rule for the voice room — the activity feed *is* voice-UI state.

type FeedEntry =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string; final: boolean }
  | { kind: 'tool'; id: string; name: string; label?: string }
  | { kind: 'error'; id: string; message: string };

type ServerMessageData =
  | { type: 'tool-started'; name: string; label?: string }
  | { type: 'assistant-text-delta'; text: string }
  | { type: 'assistant-text-final'; text: string }
  | { type: 'assistant-error'; message: string }
  | { type: 'agent-state'; state: string }
  | { type: 'user-transcript-final'; text: string };

function isServerMessage(value: unknown): value is ServerMessageData {
  if (typeof value !== 'object' || value === null) return false;
  const t = (value as { type?: unknown }).type;
  return (
    t === 'tool-started' ||
    t === 'assistant-text-delta' ||
    t === 'assistant-text-final' ||
    t === 'assistant-error' ||
    t === 'agent-state' ||
    t === 'user-transcript-final'
  );
}

let entrySeq = 0;
const nextId = (): string => `e${(++entrySeq).toString()}`;

function transcriptToEntries(transcript: TranscriptEntry[]): FeedEntry[] {
  return transcript
    .filter((e) => e.text.trim().length > 0 || Boolean(e.error))
    .map((e) => {
      if (e.error) return { kind: 'error', id: nextId(), message: e.error };
      return e.role === 'user'
        ? { kind: 'user', id: nextId(), text: e.text }
        : { kind: 'assistant', id: nextId(), text: e.text, final: true };
    });
}

export function ActivityFeed({
  initialTranscript,
}: {
  initialTranscript?: TranscriptEntry[];
} = {}): React.ReactElement {
  // Seed the feed with persisted history on mount. Live RTVI events
  // append on top — opencode replays nothing on reconnect, so any new
  // turns will simply add to whatever was already there.
  const initial = useMemo(
    () => (initialTranscript ? transcriptToEntries(initialTranscript) : []),
    // We intentionally only read this once; subsequent prop changes
    // would clobber live deltas.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [entries, setEntries] = useState<FeedEntry[]>(initial);

  const onServerMessage = useCallback((raw: unknown) => {
    // Pipecat wraps our pushed dict in `{ data: <dict> }` when it serializes
    // an RTVIServerMessageFrame. The handler argument is the *outer* object;
    // the dict we set on the python side is at `.data`.
    const inner: unknown = (raw as { data?: unknown } | null)?.data ?? raw;
    if (!isServerMessage(inner)) return;

    setEntries((prev) => {
      switch (inner.type) {
        case 'user-transcript-final': {
          const text = inner.text.trim();
          if (!text) return prev;
          return [...prev, { kind: 'user', id: nextId(), text }];
        }
        case 'tool-started':
          return [...prev, { kind: 'tool', id: nextId(), name: inner.name, label: inner.label }];
        case 'assistant-text-delta': {
          const last = prev[prev.length - 1];
          if (last?.kind === 'assistant' && !last.final) {
            const updated: FeedEntry = { ...last, text: last.text + inner.text };
            return [...prev.slice(0, -1), updated];
          }
          return [...prev, { kind: 'assistant', id: nextId(), text: inner.text, final: false }];
        }
        case 'assistant-text-final': {
          const last = prev[prev.length - 1];
          if (last?.kind === 'assistant' && !last.final) {
            // Lock in. If deltas streamed, prefer the accumulated text —
            // we may have stripped fences for TTS, but for the feed we
            // already kept everything raw via deltas. The final payload
            // is the canonical full text; use it.
            const updated: FeedEntry = { ...last, text: inner.text, final: true };
            return [...prev.slice(0, -1), updated];
          }
          // No deltas streamed (rare): synthesize a complete entry.
          return [...prev, { kind: 'assistant', id: nextId(), text: inner.text, final: true }];
        }
        case 'assistant-error':
          if (!inner.message) return prev;
          return [...prev, { kind: 'error', id: nextId(), message: inner.message }];
        case 'agent-state':
          // Surfaced by the StatusPill via its own subscription; nothing
          // to add to the feed here.
          return prev;
      }
    });
  }, []);

  useRTVIClientEvent(RTVIEvent.ServerMessage, onServerMessage);

  if (entries.length === 0) {
    return (
      <p className="px-4 py-3 text-xs text-neutral-500">
        Speak to start. Your turns, the agent's replies, and any tools it runs will show up here.
      </p>
    );
  }

  return (
    <ol className="flex flex-col gap-3 px-4 py-3">
      {entries.map((entry) => (
        <li key={entry.id} className="text-sm">
          {renderEntry(entry)}
        </li>
      ))}
    </ol>
  );
}

function renderEntry(entry: FeedEntry): React.ReactElement {
  switch (entry.kind) {
    case 'user':
      return (
        <div>
          <span className="mr-2 text-xs font-medium uppercase tracking-wide text-neutral-500">
            you
          </span>
          <span className="text-neutral-200">{entry.text}</span>
        </div>
      );
    case 'assistant':
      return (
        <div>
          <span className="mr-2 text-xs font-medium uppercase tracking-wide text-emerald-500">
            friday
          </span>
          <span className="text-neutral-100 whitespace-pre-wrap">
            {entry.text}
            {!entry.final && <span className="ml-1 animate-pulse text-neutral-500">▍</span>}
          </span>
        </div>
      );
    case 'tool':
      return (
        <div className="text-xs text-neutral-500">
          <span className="mr-1">⎿</span>
          <span className="text-neutral-300">{entry.label ?? entry.name}</span>
        </div>
      );
    case 'error':
      return (
        <div className="rounded-md border border-red-800 bg-red-950/50 px-3 py-2">
          <span className="mr-2 text-xs font-medium uppercase tracking-wide text-red-500">
            error
          </span>
          <span className="text-sm text-red-300">{entry.message}</span>
        </div>
      );
  }
}
