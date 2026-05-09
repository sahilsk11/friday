import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';

import { isApiError } from '@/lib/api';
import { useSelectedModel } from '@/lib/selectedModel';
import { getConfig, listHarnesses, listModels, listSessions } from '@/lib/sessions';
import type { HarnessInfo, ModelInfo, ModelRef } from '@/types/api';

// Plain REST. No voice-ui-kit imports here — per jarvis.md, only the
// voice room page touches the voice stack.

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

const DEFAULT_DIRECTORY = '';

function modelKey(m: ModelRef): string {
  return `${m.providerID}/${m.modelID}`;
}

function parseModelKey(key: string): ModelRef | null {
  const idx = key.indexOf('/');
  if (idx < 0) return null;
  return { providerID: key.slice(0, idx), modelID: key.slice(idx + 1) };
}

export default function SessionsList() {
  const [modalOpen, setModalOpen] = useState(false);

  const sessionsQuery = useQuery({
    queryKey: ['sessions'],
    queryFn: () => listSessions(),
    refetchOnMount: 'always',
  });

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <header className="mb-8 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">friday</h1>
        <button
          type="button"
          onClick={() => setModalOpen(true)}
          className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 hover:bg-white"
        >
          new session
        </button>
      </header>

      {sessionsQuery.isLoading ? (
        <p className="text-sm text-neutral-400">loading…</p>
      ) : sessionsQuery.error ? (
        <ErrorBanner
          message={
            isApiError(sessionsQuery.error)
              ? sessionsQuery.error.message
              : 'failed to load sessions'
          }
        />
      ) : (
        <ul className="divide-y divide-neutral-800 rounded-md border border-neutral-800">
          {(sessionsQuery.data ?? []).length === 0 ? (
            <li className="px-4 py-6 text-center text-sm text-neutral-500">
              no sessions yet — create one above
            </li>
          ) : (
            sessionsQuery.data?.map((row) => (
              <li key={row.id} className="flex items-center justify-between gap-4 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{row.title ?? row.id}</div>
                  <div className="truncate text-xs text-neutral-500">
                    {row.directory ?? '—'} · updated {formatTimestamp(row.updated_at)}
                  </div>
                </div>
                <div className="flex shrink-0 gap-2">
                  <Link
                    to={`/s/${row.id}`}
                    className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
                  >
                    voice
                  </Link>
                  <Link
                    to={`/s/${row.id}/transcript`}
                    className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
                  >
                    transcript
                  </Link>
                </div>
              </li>
            ))
          )}
        </ul>
      )}

      {modalOpen ? (
        <NewSessionModal onClose={() => setModalOpen(false)} />
      ) : null}
    </div>
  );
}

function NewSessionModal({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const [title, setTitle] = useState('');
  const [directory, setDirectory] = useState(DEFAULT_DIRECTORY);
  const directoryRef = useRef<HTMLInputElement>(null);

  const configQuery = useQuery({
    queryKey: ['config'],
    queryFn: getConfig,
    staleTime: Infinity,
  });

  useEffect(() => {
    if (configQuery.data?.defaultDirectory) {
      setDirectory(configQuery.data.defaultDirectory);
    }
  }, [configQuery.data]);

  const harnessesQuery = useQuery({
    queryKey: ['harnesses'],
    queryFn: listHarnesses,
    staleTime: 60_000,
  });

  // Default to the first available harness once loaded.
  const [harness, setHarness] = useState<string>('');
  const { model: selectedModel, setModel } = useSelectedModel(harness || null);
  useEffect(() => {
    if (harness === '' && harnessesQuery.data && harnessesQuery.data.length > 0) {
      setHarness(harnessesQuery.data[0].id);
    }
  }, [harnessesQuery.data, harness]);

  // Refetch models whenever the harness changes.
  const modelsQuery = useQuery({
    queryKey: ['models', harness],
    queryFn: () => listModels(harness || undefined),
    enabled: harness !== '',
    staleTime: 5 * 60 * 1000,
  });

  const groupedModels = useMemo<[string, { providerName: string; items: ModelInfo[] }][]>(() => {
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

  // Seed the harness model once, but don't overwrite a valid user choice.
  useEffect(() => {
    if (!modelsQuery.data?.default) return;
    if (
      selectedModel &&
      modelsQuery.data.models.some(
        (m) =>
          m.providerID === selectedModel.providerID && m.modelID === selectedModel.modelID,
      )
    ) {
      return;
    }
    setModel(modelsQuery.data.default);
  }, [modelsQuery.data, selectedModel, setModel]);

  const currentValue = selectedModel ? modelKey(selectedModel) : '';

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={() => onClose()}
    >
      <div
        className="w-full max-w-md rounded-lg border border-neutral-800 bg-neutral-950 p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-4 text-lg font-semibold">new session</h2>

        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            const d = directory.trim();
            if (!d) {
              directoryRef.current?.focus();
              return;
            }
            if (!harness) return;
            void navigate('/s/new', {
              state: { harness, directory: d, title: title.trim() || undefined },
            });
            onClose();
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-neutral-500">harness</span>
            <select
              value={harness}
              onChange={(e) => setHarness(e.target.value)}
              disabled={harnessesQuery.isLoading}
              className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm focus:border-neutral-500 focus:outline-none disabled:opacity-50"
            >
              {harnessesQuery.isLoading ? (
                <option value="">loading…</option>
              ) : harnessesQuery.error ? (
                <option value="">failed to load</option>
              ) : (
                (harnessesQuery.data ?? []).map((h: HarnessInfo) => (
                  <option key={h.id} value={h.id}>{h.name}</option>
                ))
              )}
            </select>
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-neutral-500">title</span>
            <input
              type="text"
              placeholder="optional"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm placeholder:text-neutral-500 focus:border-neutral-500 focus:outline-none"
              autoFocus
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-neutral-500">
              working directory
            </span>
            <input
              ref={directoryRef}
              type="text"
              placeholder="/absolute/path"
              value={directory}
              onChange={(e) => setDirectory(e.target.value)}
              required
              className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 font-mono text-sm placeholder:text-neutral-500 focus:border-neutral-500 focus:outline-none"
            />
            <span className="text-xs text-neutral-500">
              must be an absolute path that exists on the friday host.
            </span>
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-neutral-500">model</span>
            <select
              value={currentValue}
              onChange={(e) => {
                const next = parseModelKey(e.target.value);
                if (next) setModel(next);
              }}
              disabled={modelsQuery.isLoading || groupedModels.length === 0}
              className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm focus:border-neutral-500 focus:outline-none disabled:opacity-50"
            >
              {modelsQuery.isLoading ? (
                <option value="">loading…</option>
              ) : modelsQuery.error ? (
                <option value="">failed to load models</option>
              ) : groupedModels.length === 0 ? (
                <option value="">no tool-capable models available</option>
              ) : (
                groupedModels.map(([providerID, group]) => (
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
          </label>

          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-neutral-700 px-4 py-2 text-sm hover:border-neutral-500"
            >
              cancel
            </button>
            <button
              type="submit"
              disabled={!directory.trim() || !harness}
              className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 hover:bg-white disabled:opacity-50"
            >
              start session
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="mb-4 rounded-md border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-200">
      {message}
    </div>
  );
}
