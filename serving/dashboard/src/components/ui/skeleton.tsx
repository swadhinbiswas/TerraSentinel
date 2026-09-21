import { cn } from "@/lib/utils";

/** Placeholder that preserves layout while data loads, so nothing jumps on arrival. */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-[var(--color-surface-raised)]", className)}
      aria-hidden="true"
    />
  );
}
