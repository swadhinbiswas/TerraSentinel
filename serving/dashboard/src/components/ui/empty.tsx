import type { ReactNode } from "react";

/**
 * Empty states explain *why* rather than saying "no data".
 *
 * A blank panel is indistinguishable from a broken one. Given three marts legitimately
 * do not exist until a backfill runs, the explanation is the useful part.
 */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-dashed border-[var(--color-border-strong)] px-4 py-6 text-center">
      <p className="text-sm font-medium text-[var(--color-foreground)]">{title}</p>
      {children && <div className="mx-auto mt-1 max-w-xl text-xs leading-relaxed text-[var(--color-muted)]">{children}</div>}
    </div>
  );
}
