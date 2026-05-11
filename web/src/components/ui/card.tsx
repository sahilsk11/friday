import type { HTMLAttributes } from 'react';

import { cn } from '@/lib/utils';

type CardProps = HTMLAttributes<HTMLDivElement>;

export function Card({ className, ...props }: CardProps) {
  return (
    <div
      className={cn(
        'rounded-lg border border-[var(--border)] bg-[var(--panel)] shadow-[var(--shadow)]',
        className,
      )}
      {...props}
    />
  );
}
