import { describe, expect, it } from "vitest";
import {
  areaPath,
  bars,
  categoryTicks,
  linePath,
  linearScale,
  logScale,
  niceTicks,
} from "./svg-chart";

/**
 * Chart geometry.
 *
 * These are hand-rolled instead of a charting library, and the whole justification for that
 * is that the arithmetic is checkable. `logScale` clamping at zero and `bars` dividing by
 * an all-zero series are the two that produce an empty chart rather than a wrong one.
 */

describe("linearScale", () => {
  it("maps a domain onto a range", () => {
    const scale = linearScale([0, 10], [0, 100]);
    expect(scale(0)).toBe(0);
    expect(scale(5)).toBe(50);
    expect(scale(10)).toBe(100);
  });

  it("inverts a flipped range, which is how a y axis points upward", () => {
    const scale = linearScale([0, 10], [200, 0]);
    expect(scale(0)).toBe(200);
    expect(scale(10)).toBe(0);
  });

  it("centres a single-value series instead of dividing by zero", () => {
    // A season with one month of data would otherwise be NaN across the whole plot.
    const scale = linearScale([5, 5], [0, 100]);
    expect(scale(5)).toBe(50);
    expect(Number.isFinite(scale(5))).toBe(true);
  });

  it("carries its domain and range for callers that need them", () => {
    const scale = linearScale([1, 2], [3, 4]);
    expect(scale.domain).toEqual([1, 2]);
    expect(scale.range).toEqual([3, 4]);
  });
});

describe("logScale", () => {
  it("is logarithmic, so equal ratios are equal distances", () => {
    const scale = logScale([1, 100], [0, 200]);
    expect(scale(1)).toBeCloseTo(0, 6);
    expect(scale(100)).toBeCloseTo(200, 6);
    expect(scale(10)).toBeCloseTo(100, 6);
  });

  it("clamps a non-positive value to the floor", () => {
    // Daily detection counts include zero days, and ln(0) would put the whole series off
    // the canvas.
    const scale = logScale([1, 100], [0, 200]);
    expect(scale(0)).toBeCloseTo(scale(1), 6);
    expect(scale(-5)).toBeCloseTo(scale(1), 6);
  });

  it("extends the domain when it is degenerate rather than dividing by zero", () => {
    const scale = logScale([10, 10], [0, 100]);
    expect(Number.isFinite(scale(10))).toBe(true);
    expect(Number.isFinite(scale(0))).toBe(true);
  });
});

describe("linePath", () => {
  it("moves to the first point and lines to the rest", () => {
    const path = linePath([
      { x: 0, y: 10 },
      { x: 5, y: 20 },
      { x: 10, y: 30 },
    ]);
    expect(path).toBe("M0 10 L5 20 L10 30");
  });

  it("returns an empty string for no points, so an empty chart draws nothing", () => {
    expect(linePath([])).toBe("");
  });

  it("handles a single point without emitting a stray line command", () => {
    expect(linePath([{ x: 1, y: 2 }])).toBe("M1 2");
  });

  it("rounds, so the path string stays small", () => {
    expect(linePath([{ x: 0.123456, y: 0.987654 }])).toBe("M0.12 0.99");
  });
});

describe("areaPath", () => {
  it("closes the line down to the baseline and back", () => {
    const path = areaPath(
      [
        { x: 0, y: 10 },
        { x: 10, y: 20 },
      ],
      100,
    );
    expect(path.startsWith("M0 10 L10 20")).toBe(true);
    expect(path.endsWith("L10 100 L0 100 Z")).toBe(true);
  });

  it("returns an empty string for no points", () => {
    expect(areaPath([], 100)).toBe("");
  });
});

describe("bars", () => {
  it("lays bars out evenly across the width", () => {
    const geometry = bars([1, 2, 3, 4], 400, 100);
    expect(geometry).toHaveLength(4);
    for (const bar of geometry) {
      expect(bar.width).toBeGreaterThan(0);
      expect(bar.x).toBeGreaterThanOrEqual(0);
      expect(bar.x + bar.width).toBeLessThanOrEqual(400);
    }
    expect(geometry[0].x).toBeLessThan(geometry[3].x);
  });

  it("scales height against the largest value", () => {
    const geometry = bars([5, 10], 100, 100);
    expect(geometry[1].height).toBeCloseTo(100, 1);
    expect(geometry[0].height).toBeCloseTo(50, 1);
  });

  it("survives an all-zero series without dividing by zero", () => {
    const geometry = bars([0, 0, 0], 100, 100);
    for (const bar of geometry) {
      expect(Number.isFinite(bar.height)).toBe(true);
      expect(Number.isFinite(bar.y)).toBe(true);
    }
  });

  it("returns nothing for no values", () => {
    expect(bars([], 100, 100)).toEqual([]);
  });
});

describe("niceTicks", () => {
  it("produces round numbers inside the domain", () => {
    const ticks = niceTicks([0, 100], 4);
    expect(ticks.length).toBeGreaterThan(1);
    for (const tick of ticks) {
      expect(tick).toBeGreaterThanOrEqual(0);
      expect(tick).toBeLessThanOrEqual(100);
    }
    expect(ticks).toContain(100);
  });

  it("collapses a degenerate domain to a single tick", () => {
    expect(niceTicks([5, 5], 4)).toEqual([5]);
  });

  it("stays finite for a zero range", () => {
    for (const tick of niceTicks([0, 0], 4)) expect(Number.isFinite(tick)).toBe(true);
  });
});

describe("categoryTicks", () => {
  it("returns at most `max` ticks", () => {
    expect(categoryTicks(400, 600, 6).length).toBeLessThanOrEqual(6);
  });

  it("returns a single tick for a single point", () => {
    expect(categoryTicks(1, 100, 6)).toHaveLength(1);
  });

  it("returns nothing for no points", () => {
    expect(categoryTicks(0, 100, 6)).toEqual([]);
  });

  it("labels the index it is showing, so the axis reads real data", () => {
    const ticks = categoryTicks(10, 100, 5);
    for (const tick of ticks) {
      expect(tick.label).toBeGreaterThanOrEqual(0);
      expect(tick.label).toBeLessThan(10);
    }
  });

  it("keeps every position inside the plot width", () => {
    for (const tick of categoryTicks(37, 240, 4)) {
      expect(tick.x).toBeGreaterThanOrEqual(0);
      expect(tick.x).toBeLessThanOrEqual(240);
    }
  });
});
