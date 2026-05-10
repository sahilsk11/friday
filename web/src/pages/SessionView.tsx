import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';

import { ModelChip } from '@/components/ModelChip';
import { isApiError } from '@/lib/api';
import { useSessionEvents } from '@/lib/events';
import { useSelectedModel } from '@/lib/selectedModel';
import { getSession, postTurn } from '@/lib/sessions';
import type { AgentState } from '@/types/api';

// Plain REST + SSE. No voice-ui-kit imports here — per jarvis.md.

const STATE_LABELS: Record<AgentState, string> = {
  idle: 'idle',
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
};

const STATE_COLORS: Record<AgentState, string> = {
  idle: 'bg-neutral-700',
  listening: 'bg-blue-600',
  thinking: 'bg-amber-600',
  speaking: 'bg-emerald-600',
};

export default function SessionView() {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const transcriptEndRef = useRef<HTMLDivElement>(null);

  const sessionQuery = useQuery({
    queryKey: ['session', id],
    queryFn: () => {
      if (!id) throw new Error('missing session id');
      return getSession(id);
    },
    enabled: Boolean(id),
  });

  const live = useSessionEvents(id, sessionQuery.data?.agent_state ?? 'idle');
  const harness = sessionQuery.data?.session.harness ?? null;
  const serverModel = sessionQuery.data?.current_model ?? null;
  const persistedErrors = new Set(
    (sessionQuery.data?.transcript ?? []).flatMap((entry) => (entry.error ? [entry.error] : [])),
  );
  const persistedTranscript = sessionQuery.data?.transcript ?? [];
  const persistedAssistantTexts = persistedTranscript
    .filter((entry) => entry.role === 'assistant' && !entry.error)
    .map((entry) => entry.text);
  const syncedLiveFinals = excludePersistedLiveFinals(live.finals, persistedAssistantTexts);
  const { model: selectedModel, setModel } = useSelectedModel(harness, serverModel);

  const turnMutation = useMutation({
    mutationFn: (text: string) => {
      if (!id) throw new Error('missing session id');
      return postTurn(id, text, selectedModel ?? undefined);
    },
    onSuccess: async () => {
      setDraft('');
      // The transcript snapshot will be stale until opencode finalizes
      // the assistant turn — invalidate so the next idle event triggers
      // a refetch of the canonical view.
      await queryClient.invalidateQueries({ queryKey: ['session', id] });
    },
  });

  // Auto-scroll on new content.
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [live.pending, syncedLiveFinals.length, live.errors.length, persistedTranscript.length]);

  // When the agent returns to idle, the live `pending` buffer has
  // already been flushed via text.final. Refetch the canonical
  // transcript so historical entries render with their persisted
  // completed_at.
  useEffect(() => {
    if (live.state === 'idle' && live.connection === 'open') {
      void queryClient.invalidateQueries({ queryKey: ['session', id] });
    }
  }, [live.state, live.connection, queryClient, id]);

  if (!id) return <p className="p-6 text-sm text-red-300">missing session id</p>;

  return (
    <div className="mx-auto flex h-screen max-w-3xl flex-col px-6 py-6">
      <header className="mb-4 flex items-baseline justify-between">
        <div>
          <Link to="/" className="text-sm text-neutral-400 hover:text-neutral-200">
            ← sessions
          </Link>
          <h1 className="mt-1 truncate text-lg font-semibold">
            {sessionQuery.data?.session.title ?? id}
          </h1>
        </div>
        <div className="flex items-center gap-3">
          <ModelChip harness={harness} selected={selectedModel} onChange={setModel} />
          <StatePill state={live.state} />
          <Link
            to={`/s/${id}`}
            className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
          >
            voice
          </Link>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto rounded-md border border-neutral-800 bg-neutral-950">
        {sessionQuery.isLoading ? (
          <p className="p-6 text-sm text-neutral-400">loading…</p>
        ) : sessionQuery.error ? (
          <p className="p-6 text-sm text-red-300">
            {isApiError(sessionQuery.error)
              ? sessionQuery.error.message
              : 'failed to load transcript'}
          </p>
        ) : (
          <div className="space-y-4 p-4">
            {persistedTranscript.map((entry, i) =>
              entry.error ? (
                <ErrorBlock key={i} message={entry.error} />
              ) : (
                <TranscriptBlock key={i} role={entry.role} text={entry.text} />
              ),
            )}
            {live.pending ? <TranscriptBlock role="assistant" text={live.pending} pending /> : null}
            {syncedLiveFinals.map((text, i) => (
              <TranscriptBlock key={`live-final-${i}`} role="assistant" text={text} />
            ))}
            {live.errors
              .filter((message) => !persistedErrors.has(message))
              .map((message, i) => (
                <ErrorBlock key={`live-error-${i}`} message={message} />
              ))}
            <div ref={transcriptEndRef} />
          </div>
        )}
      </div>

      <form
        className="mt-4 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const text = draft.trim();
          if (!text) return;
          turnMutation.mutate(text);
        }}
      >
        <input
          type="text"
          placeholder="say something to the agent"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="flex-1 rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm placeholder:text-neutral-500 focus:border-neutral-500 focus:outline-none"
          disabled={turnMutation.isPending}
        />
        <button
          type="submit"
          disabled={turnMutation.isPending || !draft.trim()}
          className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 hover:bg-white disabled:opacity-50"
        >
          send
        </button>
      </form>
    </div>
  );
}

function excludePersistedLiveFinals(
  liveFinals: string[],
  persistedAssistantTexts: string[],
): string[] {
  let matchedFromEnd = 0;
  while (
    matchedFromEnd < liveFinals.length &&
    matchedFromEnd < persistedAssistantTexts.length &&
    liveFinals[liveFinals.length - 1 - matchedFromEnd] ===
      persistedAssistantTexts[persistedAssistantTexts.length - 1 - matchedFromEnd]
  ) {
    matchedFromEnd++;
  }
  return liveFinals.slice(0, liveFinals.length - matchedFromEnd);
}

function StatePill({ state }: { state: AgentState }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full bg-neutral-900 px-3 py-1 text-xs">
      <span className={`h-2 w-2 rounded-full ${STATE_COLORS[state]}`} />
      {STATE_LABELS[state]}
    </span>
  );
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-start">
      <span className="mb-1 text-xs uppercase tracking-wide text-red-500">error</span>
      <div className="max-w-[90%] whitespace-pre-wrap rounded-lg border border-red-800 bg-red-950/50 px-3 py-2 text-sm text-red-300">
        {message}
      </div>
    </div>
  );
}

function TranscriptBlock({
  role,
  text,
  pending,
}: {
  role: 'user' | 'assistant';
  text: string;
  pending?: boolean;
}) {
  const align = role === 'user' ? 'items-end' : 'items-start';
  const bubble =
    role === 'user'
      ? 'bg-neutral-800 text-neutral-100'
      : 'bg-neutral-900 text-neutral-200 border border-neutral-800';
  return (
    <div className={`flex flex-col ${align}`}>
      <span className="mb-1 text-xs uppercase tracking-wide text-neutral-500">
        {role}
        {pending ? ' · streaming' : ''}
      </span>
      <div className={`max-w-[90%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${bubble}`}>
        {text}
      </div>
    </div>
  );
}
