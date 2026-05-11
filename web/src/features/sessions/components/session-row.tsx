import { Link } from 'react-router';

import { Button } from '@/components/ui/button';
import type { SessionSummary } from '@/types/api';

interface SessionRowProps {
  session: SessionSummary;
}

export function SessionRow({ session }: SessionRowProps) {
  return (
    <li className="grid gap-4 border-t border-[var(--border)] px-5 py-4 first:border-t-0 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div>
          <h2 className="truncate text-sm font-semibold text-[var(--foreground)]">
            {session.title?.trim() || 'Untitled session'}
          </h2>
        <p className="mt-1 truncate text-sm text-[var(--muted)]">
          {formatSessionMeta(session)}
        </p>
      </div>

      <div className="flex items-center gap-2 sm:justify-end">
        <Button asChild size="sm" variant="secondary">
          <Link to={`/sessions/${session.id}`}>voice</Link>
        </Button>
        <Button asChild size="sm" variant="ghost">
          <Link to={`/sessions/${session.id}`}>transcript</Link>
        </Button>
      </div>
    </li>
  );
}

function formatSessionMeta(session: SessionSummary): string {
  const parts = [
    session.directory,
    session.updated_at || session.created_at
      ? `updated ${formatTimestamp(session.updated_at || session.created_at)}`
      : null,
  ].filter((part): part is string => Boolean(part));

  return parts.join(' · ') || session.model_id || session.harness;
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return '';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat(undefined, {
    month: 'numeric',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}
