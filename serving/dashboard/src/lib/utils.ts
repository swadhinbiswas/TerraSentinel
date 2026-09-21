import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn's class-merge helper: later Tailwind classes win over earlier ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * The intensity ramp, mirroring the CSS tokens.
 *
 * Exported as literals because maplibre paints on a canvas and cannot read CSS custom
 * properties. The ramp varies in luminance rather than hue so it survives red-green
 * colour deficiency, and every place it is used also shows the value as text or in a
 * legend — colour is a redundant channel here, never the only one.
 */
export const RAMP = [
  "oklch(0.44 0.06 265)", // below median
  "oklch(0.62 0.11 55)",
  "oklch(0.72 0.15 62)",
  "oklch(0.80 0.17 78)",
  "oklch(0.93 0.11 95)", // top 2%
] as const;

/** RGB hex equivalents, because maplibre's style expressions want parseable colours. */
export const RAMP_HEX = ["#3c4257", "#c8763a", "#e39a3f", "#ecba55", "#f6e6a8"] as const;

export function severityColor(severity: string): string {
  switch (severity) {
    case "extreme":
      return RAMP_HEX[4];
    case "high":
      return RAMP_HEX[3];
    case "moderate":
      return RAMP_HEX[2];
    case "normal":
      return RAMP_HEX[0];
    default:
      return RAMP_HEX[0];
  }
}

/** Map a value to a ramp step given data-driven breaks. */
export function rampStep(value: number, breaks: number[]): number {
  let step = 0;
  for (const boundary of breaks) {
    if (value >= boundary) step += 1;
  }
  return Math.min(step, RAMP_HEX.length - 1);
}

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}
