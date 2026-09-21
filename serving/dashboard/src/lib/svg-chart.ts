/**
 * Chart geometry, as pure functions.
 *
 * These are deliberately not a charting library. Three attempts with recharts failed in
 * three different ways — v2 predates React 19, v3 mis-measured its container, and an
 * island that fails to hydrate renders *nothing* — for charts this simple: one area series
 * with markers, one bar series, two lines. Hand-computed SVG paths:
 *
 *   * render on the server, so the first paint contains the chart and a JS failure cannot
 *     blank the panel;
 *   * add nothing to the client bundle (recharts was ~395 KB gzipped);
 *   * are testable as strings, which a canvas is not.
 *
 * Tooltips come from `<title>` elements: native, keyboard-accessible, and no JavaScript.
 */

export interface Point {
  x: number;
  y: number;
}

export interface Scale {
  (value: number): number;
  domain: [number, number];
  range: [number, number];
}

/** Linear scale with a guard against a zero-width domain (a single-value series). */
export function linearScale(domain: [number, number], range: [number, number]): Scale {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0;
  const fn = ((value: number) => {
    if (span === 0) return (r0 + r1) / 2;
    return r0 + ((value - d0) / span) * (r1 - r0);
  }) as Scale;
  fn.domain = domain;
  fn.range = range;
  return fn;
}

/** Log10 scale, for series that span orders of magnitude. Values <= 0 clamp to the floor. */
export function logScale(domain: [number, number], range: [number, number]): Scale {
  const floor = Math.max(domain[0], 0.5);
  const lo = Math.log10(floor);
  const hi = Math.log10(Math.max(domain[1], floor * 10));
  const [r0, r1] = range;
  const fn = ((value: number) => {
    const safe = Math.max(value, floor);
    return r0 + ((Math.log10(safe) - lo) / (hi - lo)) * (r1 - r0);
  }) as Scale;
  fn.domain = [floor, Math.max(domain[1], floor * 10)];
  fn.range = range;
  return fn;
}

/** ``M x y L x y ...`` for a polyline. */
export function linePath(points: Point[]): string {
  if (points.length === 0) return "";
  return points.map((point, index) => `${index === 0 ? "M" : "L"}${round(point.x)} ${round(point.y)}`).join(" ");
}

/** A line path closed to the baseline, for a filled area. */
export function areaPath(points: Point[], baseline: number): string {
  if (points.length === 0) return "";
  const first = points[0];
  const last = points[points.length - 1];
  return `${linePath(points)} L${round(last.x)} ${round(baseline)} L${round(first.x)} ${round(baseline)} Z`;
}

export interface Bar {
  x: number;
  width: number;
  y: number;
  height: number;
}

/** Bar geometry for a set of values laid out evenly across a width. */
export function bars(values: number[], width: number, height: number, gap = 2): Bar[] {
  if (values.length === 0) return [];
  const slot = width / values.length;
  const barWidth = Math.max(1, slot - gap);
  const max = Math.max(...values, 1);
  return values.map((value, index) => ({
    x: round(index * slot + gap / 2),
    width: round(barWidth),
    y: round(height - (value / max) * height),
    height: round(Math.max(0.5, (value / max) * height)),
  }));
}

/** Round tick values covering a domain, roughly ``count`` of them. */
export function niceTicks(domain: [number, number], count = 4): number[] {
  const [lo, hi] = domain;
  if (hi <= lo) return [lo];
  const rawStep = (hi - lo) / count;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rawStep) ?? magnitude * 10;
  const start = Math.ceil(lo / step) * step;
  const ticks: number[] = [];
  for (let value = start; value <= hi + step / 1000; value += step) {
    ticks.push(round(value, 6));
  }
  return ticks;
}

/** Evenly spaced tick positions for a category axis. */
export function categoryTicks(count: number, width: number, max = 6): { x: number; label: number }[] {
  if (count === 0) return [];
  const stride = Math.max(1, Math.ceil(count / max));
  const slot = width / count;
  const out: { x: number; label: number }[] = [];
  for (let index = 0; index < count; index += stride) {
    out.push({ x: round(index * slot + slot / 2), label: index });
  }
  return out;
}

function round(value: number, digits = 2): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}
