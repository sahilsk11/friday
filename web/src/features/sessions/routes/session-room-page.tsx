import { useMutation } from '@tanstack/react-query';
import { ArrowLeft, RefreshCw, ScanSearch } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router';

import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { FridayRoom } from '@/features/room';
import { joinExistingSession } from '@/features/sessions/api';
import { useSessionQuery } from '@/features/sessions/hooks';
import { getErrorMessage } from '@/lib/api';
import type { CreateSessionResponse, SessionRouteState } from '@/types/api';

export function SessionRoomPage() {
  const { sessionId } = useParams();
  const location = useLocation();
  const routeState = location.state as SessionRouteState | null;
  const [sessionPayload, setSessionPayload] = useState<CreateSessionResponse | null>(
    routeState?.sessionPayload ?? null,
  );
  const autoJoinRequestedForRef = useRef<string | null>(sessionPayload?.session_id ?? null);
  const sessionQuery = useSessionQuery(sessionId ?? '', {
    refetchInterval: sessionPayload ? 2_000 : false,
  });
  const joinMutation = useMutation({
    mutationFn: async () => {
      if (!sessionId || !sessionQuery.data) {
        throw new Error('Session metadata is not ready yet.');
      }

      return joinExistingSession(sessionId, {
        directory: sessionQuery.data.session.directory ?? '/Users/sahil/portfolio/friday-v3',
        harness: sessionQuery.data.session.harness,
        model_id: sessionQuery.data.current_model?.model_id ?? sessionQuery.data.session.model_id ?? undefined,
        title: sessionQuery.data.session.title ?? undefined,
      });
    },
    onSuccess: (response) => {
      setSessionPayload(response);
    },
  });

  useEffect(() => {
    if (
      !sessionId ||
      sessionPayload ||
      !sessionQuery.data ||
      joinMutation.isPending ||
      autoJoinRequestedForRef.current === sessionId
    ) {
      return;
    }
    autoJoinRequestedForRef.current = sessionId;
    void joinMutation.mutateAsync().catch(() => {
      autoJoinRequestedForRef.current = null;
    });
  }, [joinMutation, sessionId, sessionPayload, sessionQuery.data]);

  if (!sessionId) {
    return (
      <EmptyState
        description="The route did not include a session identifier."
        icon={ScanSearch}
        title="Missing session id"
      />
    );
  }

  if (sessionQuery.isLoading && !sessionPayload) {
    return <RoomRouteSkeleton />;
  }

  if (sessionQuery.error) {
    return (
      <EmptyState
        description={getErrorMessage(sessionQuery.error)}
        icon={ScanSearch}
        title="Unable to load session"
      />
    );
  }

  const session = sessionQuery.data?.session ?? null;
  const title = session?.title?.trim() || 'Untitled session';

  return (
    <div className="grid gap-6">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <Link className="text-sm text-[var(--muted)] hover:text-[var(--foreground)]" to="/">
            <ArrowLeft className="mr-1 inline h-4 w-4" />
            sessions
          </Link>
          <h1 className="mt-5 text-xl font-semibold text-[var(--foreground)]">
            {title}
          </h1>
          <p className="mt-1 max-w-[64ch] truncate text-sm text-[var(--muted)]">
            {session?.directory ?? 'No directory recorded yet'}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {session?.model_id ? (
            <span className="rounded-md border border-[var(--border)] px-3 py-2 text-sm text-[var(--foreground)]">
              <span className="text-[var(--muted)]">model</span> {session.model_id}
            </span>
          ) : null}
          <Button
            aria-label="Refresh LiveKit join payload"
            className="h-10 w-10 p-0"
            disabled={joinMutation.isPending || !session}
            onClick={() => {
              void joinMutation.mutateAsync();
            }}
            size="sm"
            variant="ghost"
          >
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
      </header>

      {joinMutation.error ? (
        <Card className="px-5 py-4 text-sm text-[var(--danger)]">
          {getErrorMessage(joinMutation.error)}
        </Card>
      ) : null}

      <FridayRoom
        narratorTranscript={sessionQuery.data?.narrator_transcript ?? []}
        providerLabel={sessionQuery.data?.session.harness ?? sessionPayload?.harness ?? 'provider'}
        providerTranscript={sessionQuery.data?.transcript ?? []}
        session={sessionPayload}
      />
    </div>
  );
}

function RoomRouteSkeleton() {
  return (
    <div className="grid gap-6">
      <div className="h-5 w-24 animate-pulse rounded bg-[var(--panel-muted)]" />
      <Card className="h-[70vh] animate-pulse bg-[var(--panel-muted)]" />
    </div>
  );
}
