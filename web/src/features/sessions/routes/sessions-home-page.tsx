import { Plus, RefreshCw } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { EmptyState } from '@/components/ui/empty-state';
import { NewSessionModal } from '@/features/sessions/components/new-session-modal';
import { SessionRow } from '@/features/sessions/components/session-row';
import { useHarnessesQuery, useSessionsQuery } from '@/features/sessions/hooks';
import { getErrorMessage } from '@/lib/api';

export function SessionsHomePage() {
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const sessionsQuery = useSessionsQuery();
  const harnessesQuery = useHarnessesQuery();
  const sessions = sessionsQuery.data ?? [];

  return (
    <>
      <div className="grid gap-8">
        <header className="flex items-center justify-between gap-4">
          <h1 className="text-2xl font-semibold text-[var(--foreground)]">friday</h1>
          <div className="flex items-center gap-2">
            <Button
              aria-label="Refresh sessions"
              className="h-10 w-10 p-0"
              onClick={() => {
                void sessionsQuery.refetch();
                void harnessesQuery.refetch();
              }}
              size="sm"
              variant="ghost"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            <Button onClick={() => setNewSessionOpen(true)} size="sm">
              <Plus className="h-4 w-4" />
              new session
            </Button>
          </div>
        </header>

        <section className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--panel)]">
          {sessionsQuery.error ? (
            <div className="border-b border-[var(--border)] px-5 py-4 text-sm text-[var(--danger)]">
              {getErrorMessage(sessionsQuery.error)}
            </div>
          ) : null}

          {sessionsQuery.isLoading ? (
            <ul>
              {Array.from({ length: 8 }).map((_, index) => (
                <li
                  className="grid gap-3 border-t border-[var(--border)] px-5 py-4 first:border-t-0"
                  key={index}
                >
                  <div className="h-4 w-40 animate-pulse rounded bg-[var(--panel-muted)]" />
                  <div className="h-4 w-72 max-w-full animate-pulse rounded bg-[var(--panel-muted)]" />
                </li>
              ))}
            </ul>
          ) : sessions.length ? (
            <ul>
              {sessions.map((session) => (
                <SessionRow key={session.id} session={session} />
              ))}
            </ul>
          ) : (
            <EmptyState
              className="m-5 border-0 bg-transparent"
              description="Create a session to start a LiveKit-backed Friday room."
              icon={Plus}
              title="No sessions yet"
            />
          )}
        </section>
      </div>

      <NewSessionModal onOpenChange={setNewSessionOpen} open={newSessionOpen} />
    </>
  );
}
