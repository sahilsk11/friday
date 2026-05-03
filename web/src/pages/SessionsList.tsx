import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
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

export default function SessionsList() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [title, setTitle] = useState('');

  const sessionsQuery = useQuery({
    queryKey: ['sessions'],
    queryFn: () => listSessions(),
    refetchOnMount: 'always',
  });

  const createMutation = useMutation({
    mutationFn: (t: string) => createSession(t || undefined),
    onSuccess: async (row) => {
      await queryClient.invalidateQueries({ queryKey: ['sessions'] });
      setTitle('');
      void navigate(`/s/${row.id}`);
    },
  });

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <header className="mb-8 flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">friday</h1>
        <span className="text-sm text-neutral-400">opencode sessions</span>
      </header>

      <form
        className="mb-8 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          createMutation.mutate(title.trim());
        }}
      >
        <input
          type="text"
          placeholder="new session title (optional)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="flex-1 rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm placeholder:text-neutral-500 focus:border-neutral-500 focus:outline-none"
          disabled={createMutation.isPending}
        />
        <button
          type="submit"
          disabled={createMutation.isPending}
          className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 hover:bg-white disabled:opacity-50"
        >
          {createMutation.isPending ? 'creating…' : 'new session'}
        </button>
      </form>

      {createMutation.error ? (
        <ErrorBanner
          message={
            isApiError(createMutation.error)
              ? createMutation.error.message
              : 'failed to create session'
          }
        />
      ) : null}

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
