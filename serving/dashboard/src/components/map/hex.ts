import { cellToBoundary } from "h3-js";
import type { MapCell, MapWindow } from "@/lib/queries";
import { rampStep } from "@/lib/utils";

/**
 * Geometry, projection and cell readouts, shared by both renderers.
 *
 * The hexagons are real H3 cell boundaries computed in the browser from the cell id, so
 * the database stores no polygons and a cell's shape can never go stale against the
 * data. Both the WebGL map and the canvas fallback draw from this module, which means the
 * fallback cannot quietly disagree with the primary renderer about where a cell is or what
 * it says.
 */

/** The properties both renderers read off a feature. */
export interface CellProps {
  value: number;
  step: number;
  region: string;
  metric: string;
  period: string;
  scope: string;
}

/** Longitude and latitude pairs, the order GeoJSON and `fill()` both want. */
export type Ring = number[][];

const boundaryCache = new Map<string, Ring>();

/**
 * One H3 cell's outline, memoised by cell id.
 *
 * A window can hold thousands of cells and the same cell comes back on every repaint, on
 * every window change and once more after a style reload. `cellToBoundary` allocates a
 * fresh array each call, so without the cache one window change walks the whole boundary
 * set three times over.
 */
export function boundaryFor(h3Index: string): Ring {
  const cached = boundaryCache.get(h3Index);
  if (cached) return cached;

  const ring: Ring = cellToBoundary(h3Index).map(([lat, lng]) => [lng, lat]);
  ring.push([...ring[0]]);
  boundaryCache.set(h3Index, ring);
  return ring;
}

/**
 * Cap the cache so a long session cycling through thousands of windows cannot grow it
 * without bound. H3 cells repeat heavily inside a window, so even a small cache absorbs
 * nearly all the reuse; the bound only exists for the pathological case.
 */
const BOUNDARY_CACHE_LIMIT = 20000;

export function forgetBoundaries(): void {
  if (boundaryCache.size > BOUNDARY_CACHE_LIMIT) boundaryCache.clear();
}

export function propsFor(cell: MapCell, breaks: number[]): CellProps {
  return {
    value: Number(cell.value),
    step: rampStep(Number(cell.value), breaks),
    region: cell.region_id,
    metric: cell.metric_type,
    period: String(cell.period_start).slice(0, 10),
    scope: cell.spatial_scope,
  };
}

export function toFeatureCollection(cells: MapCell[], breaks: number[]) {
  return {
    type: "FeatureCollection" as const,
    features: cells.map((cell) => ({
      type: "Feature" as const,
      properties: propsFor(cell, breaks),
      geometry: { type: "Polygon" as const, coordinates: [boundaryFor(cell.h3_index)] },
    })),
  };
}

/* Web Mercator, normalised to 0..1 on both axes. Latitude is clamped to the projection's
 * own limit rather than the data's: a cell at ±90 would send ln(tan(lat)) to infinity,
 * and both study regions sit well inside the limit anyway. */

const MERCATOR_LIMIT = 85.051129;

export function mercatorX(lng: number): number {
  return (lng + 180) / 360;
}

export function mercatorY(lat: number): number {
  const clamped = Math.max(-MERCATOR_LIMIT, Math.min(MERCATOR_LIMIT, lat));
  const rad = (clamped * Math.PI) / 180;
  return (1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2;
}

/** The unit each layer's values are counted in, for readouts and titles. */
export function unitFor(layer: MapWindow["layer"]): string {
  return layer === "sst" ? "°C anomaly" : "detections";
}

export interface Readout {
  value: string;
  where: string;
  scope: string;
}

/**
 * The text for one cell, split by line.
 *
 * These strings come out of the database, and the popup used to be assembled with
 * `setHTML`, so any value carrying markup landed in the page. The lines are produced here
 * and both renderers set them as text.
 */
export function readoutFor(props: CellProps, unit: string): Readout {
  return {
    value: `${props.value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${unit}`,
    where: [props.region, props.metric, props.period].filter(Boolean).join(" · "),
    scope: props.scope,
  };
}

/** The same three lines as DOM, for the MapLibre popup. */
export function readoutNode(readout: Readout): HTMLElement {
  const root = document.createElement("div");

  const value = document.createElement("div");
  value.style.cssText = "font-weight:600;font-size:13px";
  value.textContent = readout.value;

  const where = document.createElement("div");
  where.style.cssText = "opacity:.75;margin-top:3px";
  where.textContent = readout.where;

  const scope = document.createElement("code");
  scope.style.cssText = "opacity:.6;font-size:11px;display:block;margin-top:2px";
  scope.textContent = readout.scope;

  root.appendChild(value);
  root.appendChild(where);
  root.appendChild(scope);
  return root;
}
