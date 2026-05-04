import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';

import { isApiError } from '@/lib/api';
import { createSession, listSessions } from '@/lib/sessions';

// Plain REST. No voice-ui-kit imports here — per jarvis.md, only the
// voice room page touches the voice stack.

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

const DEFAULT_DIRECTORY = '/root/projects';

export default function SessionsList() {
  const [modalOpen, setModalOpen] = useState(false);

  const sessionsQuery = useQuery({
    queryKey: ['sessions'],
    queryFn: () => listSessions(),
    refetchOnMount: 'always',
  });

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <header className="mb-8 flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">friday</h1>
        <span className="text-sm text-neutral-400">opencode sessions</span>
      </header>

      <div className="mb-8 flex justify-end">
        <button
          type="button"
          onClick={() => {
            setModalOpen(true);
          }}
          className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 hover:bg-white"
        >
          new session
        </button>
      </div>

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
        <NewSessionModal
          onClose={() => {
            setModalOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}

function NewSessionModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [title, setTitle] = useState('');
  const [directory, setDirectory] = useState(DEFAULT_DIRECTORY);
  const directoryRef = useRef<HTMLInputElement>(null);

  const createMutation = useMutation({
    mutationFn: ({ t, d }: { t: string; d: string }) =>
      createSession(t || undefined, d || undefined),
    onSuccess: async (row) => {
      await queryClient.invalidateQueries({ queryKey: ['sessions'] });
      void navigate(`/s/${row.id}`);
    },
  });

  // Esc to close. Don't close while a request is in flight — the user
  // either gets a session back (we navigate) or an error to read.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !createMutation.isPending) {
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose, createMutation.isPending]);

  const errorMessage = createMutation.error
    ? isApiError(createMutation.error)
      ? createMutation.error.message
      : 'failed to create session'
    : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={() => {
        if (!createMutation.isPending) onClose();
      }}
    >
      <div
        className="w-full max-w-md rounded-lg border border-neutral-800 bg-neutral-950 p-6 shadow-xl"
        onClick={(e) => {
          e.stopPropagation();
        }}
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
            createMutation.mutate({ t: title.trim(), d });
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-neutral-500">title</span>
            <input
              type="text"
              placeholder="optional"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm placeholder:text-neutral-500 focus:border-neutral-500 focus:outline-none"
              disabled={createMutation.isPending}
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
              disabled={createMutation.isPending}
            />
            <span className="text-xs text-neutral-500">
              must be an absolute path that exists on the friday host.
            </span>
          </label>

          {errorMessage ? (
            <div className="rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-200">
              {errorMessage}
            </div>
          ) : null}

          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={createMutation.isPending}
              className="rounded-md border border-neutral-700 px-4 py-2 text-sm hover:border-neutral-500 disabled:opacity-50"
            >
              cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || !directory.trim()}
              className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 hover:bg-white disabled:opacity-50"
            >
              {createMutation.isPending ? 'creating…' : 'create'}
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
