import type { SelectHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * A styled native select. Deliberately not a custom listbox: the native control is
 * keyboard- and screen-reader-correct on every platform, and a bespoke popup that traps
 * focus badly is the most common regression in dashboard UI kits.
 */
export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "h-8 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 text-xs text-[var(--color-foreground)] transition-colors hover:bg-[var(--color-surface-raised)]",
        className,
      )}
      {...props}
    />
  );
}
