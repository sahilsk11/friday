import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';

import { apiClient } from '@/lib/api';
import { listModels } from '@/lib/sessions';
import type { ModelInfo, ModelRef } from '@/types/api';

// Header chip + popover for picking the model on a session. Used by both
// SessionView (REST turns) and VoiceRoom (voice turns) — they share the
// same backend state via PATCH /sessions/:id/model, so a switch in one
// applies to the next turn from either path.

export function modelKey(m: ModelRef): string {
  return `${m.providerID}/${m.modelID}`;
}

export function parseModelKey(key: string): ModelRef | null {
  const idx = key.indexOf('/');
  if (idx < 0) return null;
  return { providerID: key.slice(0, idx), modelID: key.slice(idx + 1) };
}

export function ModelChip({
  sessionId,
  active,
}: {
  sessionId: string;
  /** Model the last assistant turn ran on (or pre-first-turn staging). */
  active: ModelRef | null;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  const modelsQuery = useQuery({
    queryKey: ['models'],
    queryFn: listModels,
    staleTime: 5 * 60 * 1000,
    enabled: open,
  });

  const setModelMutation = useMutation({
    mutationFn: (m: ModelRef) =>
      apiClient.patch<void>(`/sessions/${sessionId}/model`, m),
    onSuccess: async () => {
      // The session detail caches `current_model`; refetch so the chip
      // reflects the staged choice immediately (until SSE confirms reality).
      await queryClient.invalidateQueries({ queryKey: ['session', sessionId] });
    },
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

  const labelFor = (m: ModelRef): string => {
    const found = modelsQuery.data?.models.find(
      (mi) => mi.providerID === m.providerID && mi.modelID === m.modelID,
    );
    return found?.modelName ?? m.modelID;
  };

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

  if (!active) {
    // Pre-first-turn with no staged model — keep the header clean.
    return null;
  }

  return (
    <div className="relative" ref={popoverRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-2 rounded-full border border-neutral-700 bg-neutral-900 px-3 py-1 text-xs hover:border-neutral-500"
        title={`${active.providerID}/${active.modelID}`}
      >
        <span className="text-neutral-400">model</span>
        <span className="font-medium text-neutral-100">{labelFor(active)}</span>
      </button>
      {open ? (
        <div className="absolute right-0 z-20 mt-2 w-72 rounded-md border border-neutral-800 bg-neutral-950 p-3 shadow-xl">
          <div className="mb-2 text-xs uppercase tracking-wide text-neutral-500">change model</div>
          <select
            value={modelKey(active)}
            onChange={(e) => {
              const next = parseModelKey(e.target.value);
              if (next) setModelMutation.mutate(next);
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
          <p className="mt-2 text-[11px] leading-snug text-neutral-500">
            applies to your next turn. opencode reports back which model
            actually ran each response.
          </p>
        </div>
      ) : null}
    </div>
  );
}
