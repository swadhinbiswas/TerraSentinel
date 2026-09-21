import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * Severity badges.
 *
 * Two accessibility decisions: the tone map uses the same luminance-differentiated ramp
 * as the map (not a red/green pair), and every badge carries its text label, so meaning
 * never depends on colour alone. `dot` adds a small marker for scanability without
 * replacing the word.
 */
const badge = cva("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium", {
  variants: {
    tone: {
      neutral: "bg-[var(--color-i1)]/25 text-[var(--color-foreground)]",
      moderate: "bg-[color-mix(in_oklab,var(--color-i2)_28%,transparent)] text-[var(--color-i2)]",
      high: "bg-[color-mix(in_oklab,var(--color-i3)_30%,transparent)] text-[var(--color-i3)]",
      extreme: "bg-[color-mix(in_oklab,var(--color-i5)_26%,transparent)] text-[var(--color-i5)]",
      muted: "border border-[var(--color-border)] text-[var(--color-muted)]",
      accent: "bg-[color-mix(in_oklab,var(--color-accent)_22%,transparent)] text-[var(--color-accent)]",
    },
  },
  defaultVariants: { tone: "neutral" },
});

const TONES = ["neutral", "moderate", "high", "extreme", "muted", "accent"] as const;

export function Badge({
  tone,
  dot = false,
  className,
  children,
  ...props
}: HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badge> & { dot?: boolean }) {
  // An unrecognised severity falls back to neutral rather than rendering unstyled.
  const resolved = tone && TONES.includes(tone) ? tone : "neutral";
  return (
    <span className={cn(badge({ tone: resolved }), className)} {...props}>
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />}
      {children}
    </span>
  );
}
