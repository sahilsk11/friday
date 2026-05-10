import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';

import { listModels } from '@/lib/sessions';
import type { ModelInfo, ModelRef } from '@/types/api';

// Header chip + popover for picking the model. Pure controlled component:
// the parent owns ``selected`` state and decides what to do with it (REST
// posts include it in the turn body; voice attaches it to the end-turn WS
// message). No server PATCH — the chosen model rides along on the next
// turn from whichever path sends it.

function modelKey(m: ModelRef): string {
  return `${m.providerID}/${m.modelID}`;
}

function parseModelKey(key: string): ModelRef | null {
  const idx = key.indexOf('/');
  if (idx < 0) return null;
  return { providerID: key.slice(0, idx), modelID: key.slice(idx + 1) };
}

export function ModelChip({
  harness,
  selected,
  onChange,
}: {
  harness?: string | null;
  /** Current selection. ``null`` hides the chip until the user picks one. */
  selected: ModelRef | null;
  /** Called when the user picks a different model from the popover. */
  onChange: (m: ModelRef) => void;
}) {
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  const modelsQuery = useQuery({
    queryKey: ['models', harness ?? 'default'],
    queryFn: () => listModels(harness ?? undefined),
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

  if (!selected) return null;

  return (
    <div className="relative" ref={popoverRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-2 rounded-full border border-neutral-700 bg-neutral-900 px-3 py-1 text-xs hover:border-neutral-500"
        title={`${selected.providerID}/${selected.modelID}`}
        data-testid="model-chip"
        aria-label="model"
      >
        <span className="text-neutral-400">model</span>
        <span className="font-medium text-neutral-100" data-testid="model-chip-label">{labelFor(selected)}</span>
      </button>
      {open ? (
        <div className="absolute right-0 z-20 mt-2 w-72 rounded-md border border-neutral-800 bg-neutral-950 p-3 shadow-xl">
          <div className="mb-2 text-xs uppercase tracking-wide text-neutral-500">change model</div>
          <select
            value={modelKey(selected)}
            onChange={(e) => {
              const next = parseModelKey(e.target.value);
              if (next) onChange(next);
              setOpen(false);
            }}
            disabled={modelsQuery.isLoading || grouped.length === 0}
            className="w-full rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm focus:border-neutral-500 focus:outline-none disabled:opacity-50"
            data-testid="model-select"
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
