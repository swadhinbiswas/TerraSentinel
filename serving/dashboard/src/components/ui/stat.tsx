import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * A single headline number with its label and an optional qualifier.
 *
 * The qualifier slot exists so a figure is never presented without what it is measured
 * over — "144,143 detections" is meaningless without "last 400 days".
 */
export function Stat({
  label,
  value,
  hint,
  tone = "default",
  mono = false,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "accent";
  /** Monospace the value, for identifiers like a model version. */
  mono?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("rounded-[var(--radius-card)] border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3", className)}>
      <p className="text-[11px] font-medium uppercase tracking-wider text-[var(--color-subtle)]">{label}</p>
      <p
        className={cn(
          "mt-1 font-semibold tabular-nums",
          mono ? "font-mono text-sm leading-6" : "text-xl",
          tone === "accent" && "text-[var(--color-accent)]",
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-xs text-[var(--color-muted)]">{hint}</p>}
    </div>
  );
}
