import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * Tables.
 *
 * Two decisions beyond the markup. The header is sticky, because these tables are the
 * reason a reader scrolls to them and losing the column names halfway down is the thing
 * that makes a long table unusable. And rows lift on hover, which is what makes a
 * two-column-per-row table scannable rather than a grid of grey lines.
 *
 * The header uses a surface tint instead of a border because a sticky header with a
 * transparent background shows the rows scrolling underneath it.
 */
export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return <table className={cn("w-full border-collapse text-sm", className)} {...props} />;
}

export function Th({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        "sticky top-0 z-10 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-[var(--color-subtle)]",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td
      className={cn(
        "border-b border-[var(--color-border)]/60 px-3 py-2 tabular-nums transition-colors",
        className,
      )}
      {...props}
    />
  );
}

/**
 * The scroll container a sticky header needs, plus the row hover.
 *
 * A sticky `<th>` only sticks inside a container that scrolls on both axes, and
 * `overflow-x: auto` computes `overflow-y` to `auto` as well — so this is what turns
 * the header from decoration into something that stays put while a long table scrolls.
 */
export function TableFrame({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "[&_tbody_tr]:transition-colors [&_tbody_tr:hover]:bg-[color-mix(in_oklab,var(--color-surface-raised)_60%,transparent)]",
        className,
      )}
      {...props}
    />
  );
}
