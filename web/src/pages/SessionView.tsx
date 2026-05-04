import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';

import { isApiError } from '@/lib/api';
import { useSessionEvents } from '@/lib/events';
import { getSession, listModels, postTurn } from '@/lib/sessions';
import type { AgentState, ModelInfo, ModelRef } from '@/types/api';

function modelKey(m: ModelRef): string {
  return `${m.providerID}/${m.modelID}`;
}

function parseModelKey(key: string): ModelRef | null {
  const idx = key.indexOf('/');
  if (idx < 0) return null;
  return { providerID: key.slice(0, idx), modelID: key.slice(idx + 1) };
}

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
  // Pending model override the user picked from the popover but hasn't sent
  // yet. Sent on the next turn; cleared once opencode reports the new model
  // is actually running. Null = "use whatever's already running."
  const [pendingModel, setPendingModel] = useState<ModelRef | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement>(null);

  const sessionQuery = useQuery({
    queryKey: ['session', id],
    queryFn: () => {
      if (!id) throw new Error('missing session id');
      return getSession(id);
    },
    enabled: Boolean(id),
  });

  const live = useSessionEvents(id);

  // Ground truth: prefer live SSE-reported model (always reality after the
  // first turn lands), fall back to the snapshot from GET /sessions/:id.
  const activeModel: ModelRef | null = live.model ?? sessionQuery.data?.current_model ?? null;

  // If the user picked an override and opencode confirms it's now running,
  // clear the override so the chip shows reality without the "→ X next" hint.
  useEffect(() => {
    if (pendingModel && activeModel && modelKey(pendingModel) === modelKey(activeModel)) {
      setPendingModel(null);
    }
  }, [pendingModel, activeModel]);

  const turnMutation = useMutation({
    mutationFn: (text: string) => {
      if (!id) throw new Error('missing session id');
      return postTurn(id, text, pendingModel ?? undefined);
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
  }, [live.finals.length, live.pending, sessionQuery.data?.transcript.length]);

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
          <ModelChip
            active={activeModel}
            pending={pendingModel}
            onPick={setPendingModel}
            onClearPending={() => {
              setPendingModel(null);
            }}
          />
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
            {(sessionQuery.data?.transcript ?? []).map((entry, i) => (
              <TranscriptBlock key={i} role={entry.role} text={entry.text} />
            ))}
            {live.pending ? <TranscriptBlock role="assistant" text={live.pending} pending /> : null}
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

function ModelChip({
  active,
  pending,
  onPick,
  onClearPending,
}: {
  active: ModelRef | null;
  pending: ModelRef | null;
  onPick: (m: ModelRef) => void;
  onClearPending: () => void;
}) {
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  const modelsQuery = useQuery({
    queryKey: ['models'],
    queryFn: listModels,
    staleTime: 5 * 60 * 1000,
    enabled: open,
  });

  const grouped = useMemo<[string, { providerName: string; items: ModelInfo[] }][]>(() => {
    if (!modelsQuery.data) return [];
    const out = new Map<string, { providerName: string; items: ModelInfo[] }>();
    for (const m of modelsQuery.data.models) {
      let bucket = out.get(m.providerID);
      if (!bucket) {
        bucket = { providerName: m.providerName, items: [] };
        out.set(m.providerID, bucket);
      }
      bucket.items.push(m);
    }
    return [...out.entries()];
  }, [modelsQuery.data]);

  // Resolve display names from the loaded model list when available; fall
  // back to the raw modelID otherwise (loaded lazily on first popover open).
  const labelFor = (m: ModelRef): string => {
    const found = modelsQuery.data?.models.find(
      (mi) => mi.providerID === m.providerID && mi.modelID === m.modelID,
    );
    return found?.modelName ?? m.modelID;
  };

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!popoverRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onDocClick);
    return () => {
      window.removeEventListener('mousedown', onDocClick);
    };
  }, [open]);

  const currentValue = pending
    ? modelKey(pending)
    : active
      ? modelKey(active)
      : '';

  if (!active && !pending) {
    // Pre-first-turn with no modal selection — keep header clean.
    return null;
  }

  return (
    <div className="relative" ref={popoverRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-2 rounded-full border border-neutral-700 bg-neutral-900 px-3 py-1 text-xs hover:border-neutral-500"
        title={
          pending
            ? `next turn: ${pending.providerID}/${pending.modelID}\nlast ran: ${
                active ? `${active.providerID}/${active.modelID}` : '—'
              }`
            : active
              ? `${active.providerID}/${active.modelID}`
              : ''
        }
      >
        <span className="text-neutral-400">model</span>
        <span className="font-medium text-neutral-100">
          {labelFor(pending ?? active!)}
        </span>
        {pending ? (
          <span className="text-amber-400" aria-label="pending model change">
            ●
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="absolute right-0 z-20 mt-2 w-72 rounded-md border border-neutral-800 bg-neutral-950 p-3 shadow-xl">
          <div className="mb-2 text-xs uppercase tracking-wide text-neutral-500">
            change model
          </div>
          <select
            value={currentValue}
            onChange={(e) => {
              const next = parseModelKey(e.target.value);
              if (next) onPick(next);
              setOpen(false);
            }}
            disabled={modelsQuery.isLoading || grouped.length === 0}
            className="w-full rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm focus:border-neutral-500 focus:outline-none disabled:opacity-50"
          >
            {modelsQuery.isLoading ? (
              <option value="">loading…</option>
            ) : grouped.length === 0 ? (
              <option value="">no models available</option>
            ) : (
              grouped.map(([providerID, group]) => (
                <optgroup key={providerID} label={group.providerName}>
                  {group.items.map((m) => (
                    <option key={modelKey(m)} value={modelKey(m)}>
                      {m.modelName}
                    </option>
                  ))}
                </optgroup>
              ))
            )}
          </select>
          {pending ? (
            <button
              type="button"
              onClick={() => {
                onClearPending();
                setOpen(false);
              }}
              className="mt-2 w-full rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
            >
              cancel pending change
            </button>
          ) : null}
          <p className="mt-2 text-[11px] leading-snug text-neutral-500">
            applies to your next turn. opencode reports back which model
            actually ran each response.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function StatePill({ state }: { state: AgentState }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full bg-neutral-900 px-3 py-1 text-xs">
      <span className={`h-2 w-2 rounded-full ${STATE_COLORS[state]}`} />
      {STATE_LABELS[state]}
    </span>
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
