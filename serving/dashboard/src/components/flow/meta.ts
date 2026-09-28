import type { StageState } from "@/lib/flow";

/**
 * How a stage state is presented.
 *
 * Kept out of the component so the server-rendered first paint and every later poll agree
 * on the words and the colour. A status that meant one thing on load and another after the
 * first refresh would be worse than no status at all.
 */
export const STATE_META: Record<
  StageState,
  { label: string; dot: string; text: string; border: string; surface: string }
> = {
  live: {
    label: "live",
    dot: "bg-[var(--color-i3)]",
    text: "text-[var(--color-i3)]",
    border: "border-[var(--color-i2)]/45",
    surface: "bg-[color-mix(in_oklab,var(--color-i2)_10%,transparent)]",
  },
  overdue: {
    label: "overdue",
    dot: "bg-[var(--color-i4)]",
    text: "text-[var(--color-i4)]",
    border: "border-[var(--color-i4)]/50",
    surface: "bg-[color-mix(in_oklab,var(--color-i4)_12%,transparent)]",
  },
  failed: {
    label: "failed",
    dot: "bg-[var(--color-i5)]",
    text: "text-[var(--color-i5)]",
    border: "border-[var(--color-i5)]/50",
    surface: "bg-[color-mix(in_oklab,var(--color-i5)_12%,transparent)]",
  },
  unrecorded: {
    label: "no run recorded",
    dot: "bg-[var(--color-subtle)]",
    text: "text-[var(--color-subtle)]",
    border: "border-dashed border-[var(--color-border-strong)]",
    surface: "bg-transparent",
  },
  external: {
    label: "no run of its own",
    dot: "bg-[var(--color-subtle)]",
    text: "text-[var(--color-subtle)]",
    border: "border-[var(--color-border)]",
    surface: "bg-transparent",
  },
};

/**
 * Connector class, which is not the same as node state: a stalled stage stops the wire.
 *
 * `vertical` is for the stacked layout, where the wire leaves the bottom of a card. The
 * animated sweep is horizontal, and a 160px horizontal gradient on a 1px-wide column is a
 * single flat colour, so the vertical case needs its own keyframes rather than a rotated
 * version of the same gradient.
 */
export function wireClass(state: StageState, vertical: boolean): string {
  const suffix = vertical ? "-v" : "";
  if (state === "live") return `flow-wire${suffix}`;
  if (state === "failed" || state === "overdue") return `flow-wire${suffix}-stalled`;
  return `flow-wire${suffix}-idle`;
}

const UNITS: [limit: number, seconds: number, label: string][] = [
  [60, 1, "second"],
  [3600, 60, "minute"],
  [86400, 3600, "hour"],
  [2592000, 86400, "day"],
  [31536000, 2592000, "month"],
];

/**
 * "4 hours ago", from the reader's clock rather than the server's.
 *
 * The poll brings a fresh `generatedAt` every 30 seconds but the intermediate stages keep
 * the timestamp of their last real run, so the relative time only changes when something
 * actually happened. Ticking it locally between polls is what makes a page that is not
 * moving still look alive.
 */
export function relativeTime(iso: string | null, now: number): string {
  // "never" means there is no run. An empty or unparseable string means there is a value
  // and it could not be read, which is a different claim and belongs in a different word.
  if (iso === null || iso === undefined) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 10) return "just now";
  for (const [limit, divisor, label] of UNITS) {
    if (seconds < limit) {
      const value = Math.floor(seconds / divisor);
      return `${value} ${label}${value === 1 ? "" : "s"} ago`;
    }
  }
  const years = Math.floor(seconds / 31536000);
  return `${years} year${years === 1 ? "" : "s"} ago`;
}

/** Compact duration, for a stage's last run. */
export function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  // `pipeline_runs.duration_s` measures the interval the recorder itself observed, so the
  // gold-sync step records a fraction of a second. "0.0s" reads as a broken measurement;
  // "<1s" is the same fact without inviting that conclusion.
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function compactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (Math.abs(value) < 1000) return value.toLocaleString();
  if (Math.abs(value) < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}
