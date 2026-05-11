import { ArrowLeft } from 'lucide-react';
import { Link } from 'react-router';

import { Button } from '@/components/ui/button';

export function NotFoundPage() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="max-w-md text-center">
        <p className="text-sm font-semibold uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
          Not found
        </p>
        <h1 className="mt-3 text-3xl font-semibold text-[var(--foreground)]">
          That route is not part of the scaffold.
        </h1>
        <p className="mt-4 text-sm leading-6 text-[var(--muted)]">
          The frontend shell currently owns the sessions workspace and a placeholder room route.
        </p>
        <div className="mt-6 flex justify-center">
          <Button asChild>
            <Link to="/">
              <ArrowLeft className="h-4 w-4" />
              Back to sessions
            </Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
