import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn's class-merge helper: later Tailwind classes win over earlier ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * The intensity ramp, mirrored into JS for the two canvas renderers.
 *
 * `--color-i1`..`--color-i5` are the source of truth. MapLibre paints on a WebGL canvas
 * and cannot read a CSS custom property, and the fallback renderer is a 2D canvas, so the
 * values have to exist in JS too. They are converted out of the live computed style rather
 * than copied, because a hand-copied list drifts: the literals that used to sit here had
 * already drifted from the tokens at the top two steps, and there was only one list, so
 * the light theme got the dark theme's ramp painted on white. Its top step measured about
 * 1.1:1 against the page, which is invisible.
 *
 * The ramp varies in luminance rather than hue, so it survives red-green colour
 * deficiency, and every place it is used also shows the value as text or in a legend.
 * Colour is a redundant channel here, never the only one.
 */
const RAMP_TOKENS = ["--color-i1", "--color-i2", "--color-i3", "--color-i4", "--color-i5"] as const;

/** Dark-theme values, used before the document exists and if a token cannot be read. */
const RAMP_FALLBACK = ["#425274", "#b97340", "#e68c2c", "#f9ad00", "#ffe891"];

function oklchToHex(lightness: number, chroma: number, hue: number): string {
  const radians = (hue * Math.PI) / 180;
  const a = chroma * Math.cos(radians);
  const b = chroma * Math.sin(radians);
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const channels = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  return `#${channels
    .map((channel) => {
      const clipped = Math.max(0, Math.min(1, channel));
      const encoded = clipped > 0.0031308 ? 1.055 * clipped ** (1 / 2.4) - 0.055 : 12.92 * clipped;
      return Math.round(encoded * 255).toString(16).padStart(2, "0");
    })
    .join("")}`;
}

/** The ramp for the theme currently on `<html>`, recomputed when the theme changes. */
export function rampHex(): string[] {
  if (typeof document === "undefined") return [...RAMP_FALLBACK];
  const style = getComputedStyle(document.documentElement);
  const parsed = RAMP_TOKENS.map((token) => {
    // Tailwind v4 emits these as `oklch(44% .06 265)`.
    const match = style
      .getPropertyValue(token)
      .match(/oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)\s*\)/i);
    if (!match) return null;
    return oklchToHex(
      match[2] === "%" ? Number(match[1]) / 100 : Number(match[1]),
      Number(match[3]),
      Number(match[4]),
    );
  });
  // A partially-read ramp would mis-colour half the map, so an incomplete read falls back
  // wholesale rather than mixing converted values with defaults.
  return parsed.every((value): value is string => value !== null) ? parsed : [...RAMP_FALLBACK];
}

export function severityColor(severity: string, ramp: string[] = rampHex()): string {
  switch (severity) {
    case "extreme":
      return ramp[4];
    case "high":
      return ramp[3];
    case "moderate":
      return ramp[2];
    default:
      return ramp[0];
  }
}

/** Map a value to a ramp step given data-driven breaks. */
export function rampStep(value: number, breaks: number[]): number {
  let step = 0;
  for (const boundary of breaks) {
    if (value >= boundary) step += 1;
  }
  return Math.min(step, 4);
}

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}
