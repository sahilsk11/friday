import { Slot } from '@radix-ui/react-slot';
import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { cn } from '@/lib/utils';

const buttonVariants = {
  primary:
    'bg-[var(--accent)] text-black hover:bg-[var(--accent-strong)] focus-visible:ring-[var(--accent)]',
  secondary:
    'bg-[var(--panel-strong)] text-[var(--foreground)] ring-1 ring-[var(--border-strong)] hover:bg-[var(--panel-muted)] focus-visible:ring-[var(--border-strong)]',
  ghost:
    'bg-transparent text-[var(--foreground)] ring-1 ring-[var(--border)] hover:bg-[var(--panel-muted)] focus-visible:ring-[var(--border-strong)]',
};

const buttonSizes = {
  sm: 'h-9 px-3 text-sm',
  md: 'h-11 px-4 text-sm',
  lg: 'h-12 px-5 text-sm',
};

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  asChild?: boolean;
  children: ReactNode;
  variant?: keyof typeof buttonVariants;
  size?: keyof typeof buttonSizes;
};

export function Button({
  asChild = false,
  children,
  className,
  size = 'md',
  type = 'button',
  variant = 'primary',
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot : 'button';
  const resolvedProps = asChild ? props : { type, ...props };

  return (
    <Comp
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-md font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--background)] disabled:pointer-events-none disabled:opacity-50',
        buttonVariants[variant],
        buttonSizes[size],
        className,
      )}
      {...resolvedProps}
    >
      {children}
    </Comp>
  );
}
