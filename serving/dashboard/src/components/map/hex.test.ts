import { cellToBoundary } from "h3-js";
import { describe, expect, it } from "vitest";
import {
  boundaryFor,
  forgetBoundaries,
  mercatorX,
  mercatorY,
  propsFor,
  readoutFor,
  toFeatureCollection,
  unitFor,
} from "./hex";
import type { MapCell } from "@/lib/queries";

/**
 * Map geometry and the cell readout.
 *
 * Two things here have already been wrong once. The projection fed `Math.min` a NaN when a
 * value was null, which blanked the whole chart rather than one series; and the readout was
 * built as markup from database strings, which put any value carrying HTML into the page.
 */

const cell = (overrides: Partial<MapCell> = {}): MapCell => ({
  h3_index: "883d83709ffffff",
  region_id: "iberia_fire",
  latitude: 40.4168,
  longitude: -3.7038,
  period_start: "2025-08-16T00:00:00+00:00",
  metric_type: "fire_detection_count",
  spatial_scope: "h3_res_7",
  value: 128,
  ...overrides,
});

describe("mercatorX", () => {
  it("maps the longitude range onto 0..1", () => {
    expect(mercatorX(-180)).toBeCloseTo(0, 10);
    expect(mercatorX(0)).toBeCloseTo(0.5, 10);
    expect(mercatorX(180)).toBeCloseTo(1, 10);
  });

  it("is monotonic, so east is always right of west", () => {
    expect(mercatorX(10)).toBeGreaterThan(mercatorX(5));
    expect(mercatorX(-5)).toBeLessThan(mercatorX(0));
  });
});

describe("mercatorY", () => {
  it("maps the equator to the middle and inverts with latitude", () => {
    expect(mercatorY(0)).toBeCloseTo(0.5, 10);
    // Mercator y grows downward, so north must be smaller.
    expect(mercatorY(45)).toBeLessThan(mercatorY(0));
    expect(mercatorY(-45)).toBeGreaterThan(mercatorY(0));
  });

  it("clamps at the projection limit rather than returning Infinity", () => {
    // ln(tan(90°)) is Infinity, and one cell at the pole would make every other projected
    // y NaN and blank the canvas. The study regions sit well inside the limit, so
    // clamping costs nothing and removes a whole class of failure.
    for (const lat of [90, -90, 180, -180, 1000]) {
      const y = mercatorY(lat);
      expect(Number.isFinite(y)).toBe(true);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y).toBeLessThanOrEqual(1);
    }
  });

  it("is monotonic: y decreases as latitude increases", () => {
    // Mercator y grows downward, so further north is further up the canvas. Getting this
    // backwards would render the whole map upside down, so it is pinned rather than assumed.
    expect(mercatorY(20)).toBeLessThan(mercatorY(10));
    expect(mercatorY(-10)).toBeGreaterThan(mercatorY(0));
  });
});

describe("boundaryFor", () => {
  it("returns a closed ring, so a polygon has no seam", () => {
    const ring = boundaryFor("883d83709ffffff");
    expect(ring.length).toBeGreaterThan(4);
    expect(ring[0]).toEqual(ring[ring.length - 1]);
  });

  it("swaps h3-js's [lat, lng] into [lng, lat], which GeoJSON and fill() both want", () => {
    // Getting this backwards puts every cell in the wrong hemisphere, which renders as a
    // plausible-looking empty map rather than as an error.
    const raw = cellToBoundary("883d83709ffffff");
    const ring = boundaryFor("883d83709ffffff");
    expect(ring).toHaveLength(raw.length + 1);
    for (let i = 0; i < raw.length; i += 1) {
      const [lat, lng] = raw[i];
      expect(ring[i][0]).toBeCloseTo(lng, 10);
      expect(ring[i][1]).toBeCloseTo(lat, 10);
    }
  });

  it("keeps a cell's coordinates inside the valid range", () => {
    for (const [lng, lat] of boundaryFor("883d83709ffffff")) {
      expect(lng).toBeGreaterThanOrEqual(-180);
      expect(lng).toBeLessThanOrEqual(180);
      expect(lat).toBeGreaterThanOrEqual(-90);
      expect(lat).toBeLessThanOrEqual(90);
    }
  });

  it("memoises by cell id, so repeated paints do not recompute the boundary", () => {
    const first = boundaryFor("883d83709ffffff");
    expect(boundaryFor("883d83709ffffff")).toBe(first);
  });

  it("refuses to let a caller mutate the cached ring", () => {
    const ring = boundaryFor("883d83709ffffff");
    expect(Object.isFrozen(ring)).toBe(true);
    expect(Object.isFrozen(ring[0])).toBe(true);
    // A mutation attempt throws rather than silently corrupting every later paint.
    expect(() => {
      (ring[0] as number[])[0] = 999;
    }).toThrow();
    expect(boundaryFor("883d83709ffffff")[0][0]).not.toBe(999);
  });

  it("survives being asked to forget what it knows", () => {
    boundaryFor("883d83709ffffff");
    forgetBoundaries();
    expect(boundaryFor("883d83709ffffff").length).toBeGreaterThan(4);
  });
});

describe("propsFor", () => {
  it("derives the ramp step from the data-driven breaks", () => {
    expect(propsFor(cell({ value: 1 }), [5, 78, 200, 1000]).step).toBe(0);
    expect(propsFor(cell({ value: 78 }), [5, 78, 200, 1000]).step).toBe(2);
    expect(propsFor(cell({ value: 5000 }), [5, 78, 200, 1000]).step).toBe(4);
  });

  it("coerces a value that arrived as a string", () => {
    const props = propsFor(cell({ value: "128" as unknown as number }), []);
    expect(props.value).toBe(128);
  });

  it("truncates the period to a date", () => {
    expect(propsFor(cell(), []).period).toBe("2025-08-16");
  });
});

describe("toFeatureCollection", () => {
  it("produces one polygon feature per cell", () => {
    const collection = toFeatureCollection([cell(), cell({ h3_index: "883d8370bffffff" })], [
      5, 78, 200, 1000,
    ]);
    expect(collection.type).toBe("FeatureCollection");
    expect(collection.features).toHaveLength(2);
    expect(collection.features[0].geometry.type).toBe("Polygon");
    expect(collection.features[0].geometry.coordinates[0].length).toBeGreaterThan(4);
  });

  it("returns an empty collection rather than throwing on no cells", () => {
    expect(toFeatureCollection([], []).features).toHaveLength(0);
  });
});

describe("readoutFor", () => {
  const props = propsFor(cell(), []);

  it("includes the value, the unit and where the cell is", () => {
    const readout = readoutFor(props, "detections");
    expect(readout.value).toContain("128");
    expect(readout.value).toContain("detections");
    expect(readout.where).toContain("iberia_fire");
    expect(readout.where).toContain("2025-08-16");
  });

  it("carries no markup, because the strings come from the database", () => {
    // This is the reason the readout is text and not HTML. A cell whose region contained
    // a tag used to be injected into the popup verbatim.
    const hostile = "<img src=x onerror=alert(1)>";
    const readout = readoutFor({ ...props, region: hostile }, "detections");
    // Passed through verbatim as a string. Both renderers set it as text, so the tag is
    // displayed rather than parsed; the guarantee is that it is a plain string here.
    expect(readout.where).toContain(hostile);
    expect(typeof readout.where).toBe("string");
  });

  it("skips an empty field rather than leaving a dangling separator", () => {
    const readout = readoutFor({ ...props, metric: "", period: "" }, "detections");
    expect(readout.where).toBe("iberia_fire");
  });
});

describe("unitFor", () => {
  it("names the unit each layer's values are counted in", () => {
    expect(unitFor("fire")).toBe("detections");
    expect(unitFor("sst")).toBe("°C anomaly");
  });
});
