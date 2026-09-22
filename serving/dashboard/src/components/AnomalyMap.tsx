import { cellToBoundary } from "h3-js";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MapCell, MapWindow } from "@/lib/queries";
import { formatNumber, RAMP_HEX, rampStep } from "@/lib/utils";
import { Empty } from "@/components/ui/empty";
import { Select } from "@/components/ui/select";
import { Tabs } from "@/components/ui/tabs";
import "maplibre-gl/dist/maplibre-gl.css";

/**
 * Anomaly map.
 *
 * The first version of this showed "the last 30 days" with a hardcoded colour scale and
 * looked empty. Both were real bugs:
 *
 *   * The record's largest events are historical — a 13,329-detection day in August 2025 —
 *     so a recent-window default systematically showed the quiet tail of the season.
 *     Period presets are anchored to the documented events instead.
 *   * A fixed 1/10/100/1000 scale painted a typical cell (value ~3) near-black on a dark
 *     basemap. The scale is now derived from quantiles of whatever is on screen, and the
 *     legend prints the actual break values rather than implying them.
 *
 * Hexagons are drawn from real H3 cell boundaries: a res-7 cell is ~5 km² and a res-5 cell
 * is ~253 km², so a uniform dot would imply precision the coarse layer does not have.
 */

/** Presets anchored to measured events rather than to calendar windows. */
const PERIODS = [
  { value: "recent", label: "Last 30 days", from: (today: string) => shiftDays(today, -30), to: (today: string) => today, hint: "The tail of the season — quiet by design" },
  { value: "iberia2025", label: "Aug 2025 megafire", from: () => "2025-08-13", to: () => "2025-08-19", hint: "13,329 detections on the peak day" },
  { value: "winter2026", label: "Feb 2026 anomaly", from: () => "2026-02-22", to: () => "2026-02-28", hint: "1,184 detections against a winter median of 66" },
  { value: "season2026", label: "2026 season", from: () => "2026-06-01", to: () => "2026-09-20", hint: "The full fire season" },
  { value: "all", label: "Full record", from: () => "2024-09-01", to: () => "2030-01-01", hint: "Every cell in the archive" },
] as const;

/** Scoping to one region is what makes a window legible: spanning both study regions puts
 *  the camera 38 degrees wide, where 1 km cells are sub-pixel. */
const REGIONS = [
  { value: "iberia_fire", label: "Iberia" },
  { value: "greece_fire", label: "Greece" },
  { value: "all", label: "Both regions" },
] as const;

type PeriodValue = (typeof PERIODS)[number]["value"];

function shiftDays(iso: string, days: number): string {
  const date = new Date(`${iso}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function toFeatureCollection(cells: MapCell[], breaks: number[]) {
  return {
    type: "FeatureCollection" as const,
    features: cells.map((cell) => {
      const ring = cellToBoundary(cell.h3_index).map(([lat, lng]) => [lng, lat]);
      ring.push(ring[0]);
      return {
        type: "Feature" as const,
        properties: {
          value: cell.value,
          step: rampStep(Number(cell.value), breaks),
          region: cell.region_id,
          metric: cell.metric_type,
          period: String(cell.period_start).slice(0, 10),
          scope: cell.spatial_scope,
        },
        geometry: { type: "Polygon" as const, coordinates: [ring] },
      };
    }),
  };
}

/**
 * Paint the data and frame the camera on it.
 *
 * The framing is always instantaneous, which is a bug fix rather than a preference:
 * `fitBounds` with a duration is an *animation*, and an animation that never runs leaves
 * the camera on its default centre — which is how this map managed to render nothing while
 * holding 4,392 features. Verified by inspecting the live map: moving the viewport onto
 * the data turns 0 rendered features into 3,901.
 */
function paint(instance: MapLibreMap, payload: MapWindow) {
  const source = instance.getSource("cells") as GeoJSONSource | undefined;
  source?.setData(toFeatureCollection(payload.cells, payload.breaks) as never);

  if (payload.cells.length === 0) return;

  const lngs = payload.cells.map((cell) => cell.longitude);
  const lats = payload.cells.map((cell) => cell.latitude);
  const bounds: [[number, number], [number, number]] = [
    [Math.min(...lngs) - 0.5, Math.min(...lats) - 0.5],
    [Math.max(...lngs) + 0.5, Math.max(...lats) + 0.5],
  ];

  const fit = () => {
    const singlePoint = bounds[0][0] === bounds[1][0] && bounds[0][1] === bounds[1][1];
    if (singlePoint) {
      instance.jumpTo({ center: bounds[0], zoom: 8 });
      return;
    }
    instance.fitBounds(bounds, { padding: 36, duration: 0, maxZoom: 9 });
  };

  // The container often has no size when the map loads — the stylesheet that gives the
  // grid its columns may not have applied yet — and fitting a zero-width box produces a
  // degenerate camera, which is how this map rendered nothing while holding 4,392
  // features. Waiting on the map's own `idle` event does not help: an empty map never
  // reaches idle. Waiting a fixed number of frames does not either, because the stylesheet
  // can take longer than any bound.
  //
  // Observing the container instead means the fit happens the moment there is a size to
  // fit to, whenever that is.
  const container = instance.getContainer();
  if (container.clientWidth > 0) {
    fit();
    return;
  }
  const observer = new ResizeObserver(() => {
    if (container.clientWidth > 0) {
      observer.disconnect();
      fit();
    }
  });
  observer.observe(container);
  // Last resort if the element never resizes (display:none, or an odd embedding).
  window.setTimeout(() => {
    observer.disconnect();
    if (container.clientWidth > 0) fit();
  }, 4000);
}

const LAYERS = { fire: "Fire detections", sst: "SST anomaly" } as const;
type LayerKey = keyof typeof LAYERS;

export default function AnomalyMap({ initial }: { initial: MapWindow }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);

  const [layer, setLayer] = useState<LayerKey>("fire");
  const [period, setPeriod] = useState<PeriodValue>("iberia2025");
  const [region, setRegion] = useState<string>("iberia_fire");
  const [mode, setMode] = useState<"all" | "anomalies">("all");
  const [data, setData] = useState<MapWindow>(initial);
  const [loading, setLoading] = useState(false);
  // The map's `load` event fires *after* the first data effect has already run, so a
  // handler that initialises the source from a stale closure would paint an empty
  // collection and the first render would show nothing. The ref carries whatever is
  // current into the handler whenever it fires.
  const latest = useRef<MapWindow>(initial);

  const today = useMemo(() => new Date().toISOString().slice(0, 10), []);
  const selected = PERIODS.find((item) => item.value === period) ?? PERIODS[1];

  const load = useCallback(
    async (nextLayer: LayerKey, nextPeriod: PeriodValue, nextMode: "all" | "anomalies", nextRegion: string) => {
      const preset = PERIODS.find((item) => item.value === nextPeriod) ?? PERIODS[1];
      setLoading(true);
      try {
        const params = new URLSearchParams({
          layer: nextLayer,
          from: preset.from(today),
          to: preset.to(today),
          mode: nextMode,
        });
        if (nextRegion !== "all") params.set("region", nextRegion);
        const payload = (await (await fetch(`/api/map?${params}`)).json()) as MapWindow;
        latest.current = payload;
        setData(payload);
      } finally {
        setLoading(false);
      }
    },
    [today],
  );

  // Changing any control refetches; the map instance is created once and never recreated.
  useEffect(() => {
    void load(layer, period, mode, region);
  }, [layer, period, mode, region, load]);

  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new maplibregl.Map({
      container: container.current,
      // A free, keyless basemap. If it fails, the hexagons still render on the
      // background colour, so the map degrades instead of disappearing.
      // If the basemap fetch fails, the data layers still render on the container's
      // background rather than leaving an unstyled void that reads as a broken map.
      style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
      center: [-3, 42],
      zoom: 6,
      attributionControl: { compact: true },
    });
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    instance.addControl(new maplibregl.ScaleControl({ maxWidth: 90 }), "bottom-left");

    instance.on("load", () => {
      // Created with the real data, not an empty placeholder: a source added empty and
      // filled in the same tick can end up with no features rendered, which is the state
      // this map shipped in once.
      instance.addSource("cells", {
        type: "geojson",
        data: toFeatureCollection(latest.current.cells, latest.current.breaks),
      });
      // Dual encoding, and it is not decorative. Measured: an H3 res-7 cell (~1.1 km
      // across) is 0.23 px at zoom 5 and still under 4 px at zoom 9. Drawing only
      // polygons means the map looks empty at every zoom a region-wide view needs — which
      // is exactly how this page shipped once already.
      //
      //   dots      always visible, radius floor of a few pixels, so activity reads at any
      //             zoom and a single-detection cell is never lost
      //   polygons  real cell boundaries, from zoom 7 up, where the shape is legible
      //
      // Colour carries the same data in both, and the legend prints the actual values, so
      // neither the dot nor the polygon is the only channel.
      instance.addLayer({
        id: "cells-point",
        type: "circle",
        source: "cells",
        paint: {
          "circle-color": [
            "match",
            ["get", "step"],
            0, RAMP_HEX[0],
            1, RAMP_HEX[1],
            2, RAMP_HEX[2],
            3, RAMP_HEX[3],
            RAMP_HEX[4],
          ],
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["zoom"],
            // At region scale the floor dominates, so cells stay individually visible.
            3, ["+", 2.5, ["*", ["get", "step"], 0.9]],
            8, ["+", 4.5, ["*", ["get", "step"], 2.2]],
            13, ["+", 7, ["*", ["get", "step"], 4]],
          ],
          "circle-opacity": ["interpolate", ["linear"], ["zoom"], 3, 0.8, 10, 0.9],
          "circle-stroke-color": "rgba(0,0,0,0.45)",
          "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 3, 0.4, 10, 0.8],
        },
      });
      instance.addLayer({
        id: "cells-fill",
        type: "fill",
        source: "cells",
        minzoom: 7,
        paint: {
          "fill-color": [
            "match",
            ["get", "step"],
            0, RAMP_HEX[0],
            1, RAMP_HEX[1],
            2, RAMP_HEX[2],
            3, RAMP_HEX[3],
            RAMP_HEX[4],
          ],
          "fill-opacity": 0.75,
        },
      });
      instance.addLayer({
        id: "cells-outline",
        type: "line",
        source: "cells",
        minzoom: 8,
        paint: { "line-color": "rgba(255,255,255,0.28)", "line-width": 0.6 },
      });

      const hitLayers = ["cells-point", "cells-fill"];

      instance.on("click", hitLayers, (event) => {
        const feature = event.features?.[0];
        if (!feature) return;
        const props = feature.properties as Record<string, string>;
        const unit = layer === "fire" ? "detections" : "°C anomaly";
        new maplibregl.Popup({ closeButton: true })
          .setLngLat(event.lngLat)
          .setHTML(
            `<div style="font-weight:600;font-size:13px">${Number(props.value).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${unit}</div>` +
              `<div style="opacity:.75;margin-top:3px">${props.region}</div>` +
              `<div style="opacity:.75">${props.period}</div>` +
              `<code style="opacity:.6;font-size:11px">${props.scope}</code>`,
          )
          .addTo(instance);
      });
      instance.on("mouseenter", hitLayers, () => {
        instance.getCanvas().style.cursor = "pointer";
      });
      instance.on("mouseleave", hitLayers, () => {
        instance.getCanvas().style.cursor = "";
      });

      // Paint now that layers exist (the data effect that ran before load had nowhere to
      // put its features), then re-issue on the next frame. The second call is not
      // redundancy: a setData issued in the same tick as the source is created can be
      // dropped before the source has built its tiles.
      paint(instance, latest.current);
      requestAnimationFrame(() => paint(instance, latest.current));
    });

    map.current = instance;
    // Exposed deliberately for diagnostics. A maplibre canvas that renders nothing is a
    // black box from the outside, and being able to inspect the source, layer list and
    // viewport from devtools is the difference between diagnosing that and guessing at it.
    (window as unknown as Record<string, unknown>).__terrasentinelMap = instance;

    return () => {
      instance.remove();
      map.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Paint on every data change. Also framed here rather than only on load, because a
  // window change should reframe as well.
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;
    paint(instance, data);
  }, [data]);

  const peak = data.peak;
  const hasCells = data.cells.length > 0;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-4 py-2.5">
        <Select value={period} onChange={(event) => setPeriod(event.target.value as PeriodValue)} aria-label="Period">
          {PERIODS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </Select>
        <Select value={region} onChange={(event) => setRegion(event.target.value)} aria-label="Region">
          {REGIONS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </Select>
        <Tabs
          items={[
            { value: "all", label: "All cells" },
            {
              value: "anomalies",
              label: data.grain === "month" ? "Anomalous months" : "Anomalous days",
              hint:
                data.grain === "month"
                  ? "Only cells in months that contain a flagged day"
                  : "Only cells on days the detector flagged",
            },
          ]}
          value={mode}
          onChange={setMode}
        />
        <div className="ml-auto flex items-center gap-3 text-xs text-[var(--color-muted)]">
          <Tabs
            items={[
              { value: "fire", label: LAYERS.fire, hint: "H3 res 7 · ~5 km² · daily" },
              { value: "sst", label: LAYERS.sst, hint: "H3 res 5 · ~253 km² · monthly" },
            ]}
            value={layer}
            onChange={setLayer}
            size="sm"
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs text-[var(--color-muted)]">
        <span>{selected.hint}</span>
        <span aria-hidden="true">·</span>
        <span className="tabular-nums">
          {formatNumber(data.totalCells)} cells
          {data.truncated && ` (showing the densest ${formatNumber(data.cells.length)})`}
        </span>
        <span aria-hidden="true">·</span>
        <span className="tabular-nums">
          {data.from} → {data.to}
        </span>
        <span aria-hidden="true">·</span>
        <span>
          {data.grain === "month"
            ? "monthly resolution — each cell is stamped with the first of its month"
            : "daily resolution"}
        </span>
        {peak && (
          <>
            <span aria-hidden="true">·</span>
            <span className="tabular-nums">
              densest cell {formatNumber(peak.value)} in {peak.region_id}
            </span>
          </>
        )}
        {loading && <span className="ml-auto">loading…</span>}
      </div>

      <div className="relative">
        <div ref={container} className="h-[500px] w-full bg-[var(--color-background)]" />
        {!hasCells && !loading && (
          <div className="absolute inset-x-6 top-6">
            <Empty title="No cells in this window">
              <p>
                {mode === "anomalies"
                  ? data.grain === "month"
                    ? "No month in this range contains a flagged day. The window has been widened to whole months already, so try “All cells”, or a period anchored to a documented event such as August 2025."
                    : "No day in this range was flagged. Try “All cells”, or a period anchored to a documented event such as August 2025."
                  : "This window has no data. The Sentinel layers are also absent until the Earth Engine backfill runs."}
              </p>
              {data.coverage.from !== "" &&
                (data.to < data.coverage.from || data.from > data.coverage.to) && (
                  <p className="mt-2">
                    The requested window ({data.from} → {data.to}) falls outside this layer's
                    coverage, {data.coverage.from} → {data.coverage.to}
                    {data.grain === "month"
                      ? " — the SST mart is monthly and its backfill is still filling in earlier periods."
                      : "."}
                  </p>
                )}
            </Empty>
          </div>
        )}
      </div>

      <Legend breaks={data.breaks} domain={data.domain} layer={layer} grain={data.grain} />
      <ActivityStrip activity={data.activity} breaks={data.breaks} grain={data.grain} />
    </div>
  );
}

/** The legend prints the real break values, so the colour scale is checkable rather than
 *  implied. It also states the cell resolution the colours are drawn on, and — because it
 *  changes what a value *means* — the grain the layer is stored at. */
function Legend({
  breaks,
  domain,
  layer,
  grain,
}: {
  breaks: number[];
  domain: { min: number; max: number };
  layer: LayerKey;
  grain: MapWindow["grain"];
}) {
  const labels = [
    `< ${formatNumber(breaks[0])}`,
    `${formatNumber(breaks[0])}–${formatNumber(breaks[1])}`,
    `${formatNumber(breaks[1])}–${formatNumber(breaks[2])}`,
    `${formatNumber(breaks[2])}–${formatNumber(breaks[3])}`,
    `≥ ${formatNumber(breaks[3])}`,
  ];

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-[var(--color-border)] px-4 py-2.5 text-xs">
      <span className="text-[var(--color-muted)]">
        {layer === "fire" ? "Detections per cell" : "Monthly SST anomaly per cell"}
        <span className="ml-1 text-[var(--color-subtle)]">
          (quantiles of what is shown{grain === "month" ? " · monthly means" : ""})
        </span>
      </span>
      <div className="flex items-center gap-2">
        <div className="flex overflow-hidden rounded border border-[var(--color-border)]">
          {RAMP_HEX.map((colour) => (
            <span key={colour} className="h-3 w-7" style={{ background: colour }} aria-hidden="true" />
          ))}
        </div>
        <span className="tabular-nums text-[var(--color-muted)]">
          {labels[0]} … {labels[4]}
        </span>
      </div>
      <span className="tabular-nums text-[var(--color-muted)]">
        range {formatNumber(domain.min)}–{formatNumber(domain.max)}
      </span>
      <span className="ml-auto text-[var(--color-subtle)]">
        {layer === "fire" ? "H3 res 7 · ~5 km² cells" : "H3 res 5 · ~253 km² cells"}
      </span>
    </div>
  );
}

/**
 * Cells per period across the window.
 *
 * This is what makes the events findable: on a map of a whole season the peak day is one
 * among many, and a reader has no way to know which day to look at. The strip shows where
 * the activity is, and the tallest bar is usually the event of interest.
 *
 * The label tracks the layer's grain: on the monthly SST layer each bar *is* a month, and
 * calling that a day would be a small lie the reader has no way to catch.
 */
function ActivityStrip({
  activity,
  breaks,
  grain,
}: {
  activity: MapWindow["activity"];
  breaks: number[];
  grain: MapWindow["grain"];
}) {
  if (activity.length === 0) return null;
  const peakCells = Math.max(...activity.map((row) => row.cells));
  const busiest = activity.find((row) => row.cells === peakCells);

  if (activity.length < 2) return null;

  const unit = grain === "month" ? "month" : "day";

  return (
    <div className="border-t border-[var(--color-border)] px-4 py-3">
      <div className="mb-1.5 flex items-baseline justify-between text-xs">
        <span className="text-[var(--color-muted)]">Cells per {unit}</span>
        {busiest && (
          <span className="tabular-nums text-[var(--color-subtle)]">
            busiest {busiest.period_start}: {formatNumber(busiest.cells)} cells, peak {formatNumber(busiest.peak)}
          </span>
        )}
      </div>
      <div
        className="flex h-12 items-end gap-px"
        role="img"
        aria-label={`Cells per ${unit}, busiest ${busiest?.period_start}`}
      >
        {activity.map((row) => {
          const height = Math.max(2, Math.round((row.cells / peakCells) * 100));
          return (
            <span
              key={row.period_start}
              title={`${row.period_start}: ${formatNumber(row.cells)} cells, peak ${formatNumber(row.peak)}`}
              className="flex-1 rounded-t-sm"
              style={{
                height: `${height}%`,
                background: rampStep(row.peak, breaks) >= 3 ? RAMP_HEX[4] : RAMP_HEX[Math.max(1, rampStep(row.peak, breaks))],
                opacity: 0.85,
              }}
            />
          );
        })}
      </div>
    </div>
  );
}
