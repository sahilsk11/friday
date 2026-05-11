import type { PropsWithChildren } from 'react';

import { cn } from '@/lib/utils';

const badgeVariants = {
  neutral: 'bg-[var(--panel-muted)] text-[var(--muted)] ring-[var(--border)]',
  accent: 'bg-[var(--accent-soft)] text-[var(--accent-strong)] ring-[rgba(15,118,110,0.18)]',
  warning: 'bg-[rgba(217,119,6,0.12)] text-[var(--warning)] ring-[rgba(217,119,6,0.18)]',
};

type BadgeProps = PropsWithChildren<{
  variant?: keyof typeof badgeVariants;
  className?: string;
}>;

export function Badge({ children, className, variant = 'neutral' }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ring-inset',
        badgeVariants[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}
