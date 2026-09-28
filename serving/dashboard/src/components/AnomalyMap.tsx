import maplibregl, {
  type GeoJSONSource,
  type Map as MapLibreMap,
  type StyleSpecification,
} from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MapWindow } from "@/lib/queries";
import { useTheme, type Theme } from "@/lib/theme";
import { formatNumber, rampHex } from "@/lib/utils";
import { Empty } from "@/components/ui/empty";
import { Select } from "@/components/ui/select";
import { Tabs } from "@/components/ui/tabs";
import HexCanvas from "@/components/map/HexCanvas";
import { forgetBoundaries, readoutFor, readoutNode, toFeatureCollection, unitFor } from "@/components/map/hex";
import { webglUsable } from "@/components/map/webgl";
import "maplibre-gl/dist/maplibre-gl.css";

/**
 * Anomaly map.
 *
 * The map has two renderers. MapLibre draws the real thing on WebGL, and when the
 * browser will not issue a WebGL context — hardware acceleration off, a blocklisted
 * driver, a VM, a headless browser — it throws out of its own constructor and the card
 * goes blank. Canvas 2D draws the same cells when that happens. Which one is running is
 * stated in the interface rather than left to be inferred from a blank rectangle.
 *
 * Two earlier bugs shaped the rest:
 *
 *   * The record's largest events are historical, a 13,329-detection day in August 2025,
 *     so a recent-window default shows the quiet tail of the season. The period presets
 *     are anchored to the documented events instead.
 *   * A fixed 1/10/100/1000 scale painted a typical cell (value around 3) near-black on a
 *     dark basemap. The scale is derived from quantiles of whatever is on screen, and the
 *     legend prints the real break values rather than implying them.
 *
 * Hexagons are real H3 cell boundaries: a res-7 cell is about 5 km² and a res-5 cell
 * about 253 km², so a uniform dot would claim precision the coarse layer does not have.
 */

/** Presets anchored to measured events rather than to calendar windows. */
const PERIODS = [
  {
    value: "recent",
    label: "Last 30 days",
    from: (today: string) => shiftDays(today, -30),
    to: (today: string) => today,
    hint: "the end of the season, which is quiet",
  },
  {
    value: "iberia2025",
    label: "Aug 2025 megafire",
    from: () => "2025-08-13",
    to: () => "2025-08-19",
    hint: "13,329 detections on the peak day",
  },
  {
    value: "winter2026",
    label: "Feb 2026 anomaly",
    from: () => "2026-02-22",
    to: () => "2026-02-28",
    hint: "1,184 detections against a winter median of 66",
  },
  {
    value: "season2026",
    label: "2026 season",
    from: () => "2026-06-01",
    to: () => "2026-09-20",
    hint: "the whole fire season",
  },
  {
    value: "all",
    label: "Full record",
    from: () => "2024-09-01",
    to: () => "2030-01-01",
    hint: "every cell in the archive",
  },
] as const;

/** Scoping to one region is what makes a window legible: spanning both study regions puts
 *  the camera 38 degrees wide, where 1 km cells are sub-pixel. */
const REGIONS = [
  { value: "iberia_fire", label: "Iberia" },
  { value: "greece_fire", label: "Greece" },
  { value: "all", label: "Both regions" },
] as const;

const LAYERS = { fire: "Fire detections", sst: "SST anomaly" } as const;
type LayerKey = keyof typeof LAYERS;
type Engine = "webgl" | "canvas" | null;

/** Keyless, free basemaps. One per theme, so the map stops being a dark hole in a light page. */
const BASEMAP = {
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
} as const;

const BACKDROP = { dark: "#11151d", light: "#eceef2" } as const;

/** A style with no sources, for when the basemap cannot be fetched at all. */
function blankStyle(theme: keyof typeof BACKDROP): StyleSpecification {
  return {
    version: 8,
    sources: {},
    layers: [
      { id: "ts-backdrop", type: "background", paint: { "background-color": BACKDROP[theme] } },
    ],
  };
}

/** One ramp lookup, shared by the fill and the point layer so they cannot disagree. */
function rampExpression(ramp: string[]): maplibregl.ExpressionSpecification {
  return ["match", ["get", "step"], 0, ramp[0], 1, ramp[1], 2, ramp[2], 3, ramp[3], ramp[4]];
}

type PeriodValue = (typeof PERIODS)[number]["value"];

function shiftDays(iso: string, days: number): string {
  const date = new Date(`${iso}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

/**
 * Add the data source, its layers and its pointer handlers.
 *
 * Idempotent, and it has to be: a theme change calls `setStyle`, which drops every
 * layer the previous style owned, and MapLibre's per-layer pointer handlers go with
 * them. Returning early when the source is already there is what keeps the handlers
 * from being attached twice on the initial load.
 */
function attachData(
  instance: MapLibreMap,
  payload: MapWindow,
  unit: () => string,
  theme: Theme,
) {
  if (instance.getSource("cells")) return;
  const ramp = rampHex();

  instance.addSource("cells", {
    type: "geojson",
    data: toFeatureCollection(payload.cells, payload.breaks) as never,
  });

  // Dual encoding, and it is not decoration. Measured: an H3 res-7 cell (about 1.1 km
  // across) is 0.23 px at zoom 5 and still under 4 px at zoom 9. Drawing only polygons
  // means the map looks empty at every zoom a region-wide view needs.
  //
  //   dots      always visible, with a pixel floor, so activity reads at any zoom and a
  //             single-detection cell is never lost
  //   polygons  the real cell boundaries, from zoom 7 up, where the shape is legible
  //
  // Colour carries the same data in both, and the legend prints the actual values.
  //
  // The dot's stroke is what makes the bottom of the ramp visible at all. Its fill is a
  // dark slate chosen to sit below the median, which on a dark basemap is very close to
  // the background; a dark stroke on top of that is invisible. A light stroke gives every
  // cell an edge, so a single-detection cell reads as a ring rather than vanishing.
  instance.addLayer({
    id: "cells-point",
    type: "circle",
    source: "cells",
    paint: {
      "circle-color": rampExpression(ramp),
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["zoom"],
        3,
        ["+", 2.5, ["*", ["get", "step"], 0.9]],
        8,
        ["+", 4.5, ["*", ["get", "step"], 2.2]],
        13,
        ["+", 7, ["*", ["get", "step"], 4]],
      ],
      "circle-opacity": ["interpolate", ["linear"], ["zoom"], 3, 0.85, 10, 0.92],
      "circle-stroke-color": theme === "dark" ? "rgba(226,232,240,0.4)" : "rgba(15,23,42,0.35)",
      "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 3, 0.5, 10, 0.9],
    },
  });
  instance.addLayer({
    id: "cells-fill",
    type: "fill",
    source: "cells",
    minzoom: 7,
    paint: { "fill-color": rampExpression(ramp), "fill-opacity": 0.75 },
  });
  instance.addLayer({
    id: "cells-outline",
    type: "line",
    source: "cells",
    minzoom: 8,
    paint: {
      "line-color": theme === "dark" ? "rgba(226,232,240,0.3)" : "rgba(15,23,42,0.25)",
      "line-width": 0.6,
    },
  });

  const hitLayers = ["cells-point", "cells-fill"];

  instance.on("click", hitLayers, (event) => {
    const feature = event.features?.[0];
    if (!feature) return;
    const props = feature.properties as Record<string, string>;
    new maplibregl.Popup({ closeButton: true, maxWidth: "260px" })
      .setLngLat(event.lngLat)
      .setDOMContent(
        readoutNode(
          readoutFor(
            {
              value: Number(props.value),
              step: Number(props.step),
              region: props.region,
              metric: props.metric,
              period: props.period,
              scope: props.scope,
            },
            unit(),
          ),
        ),
      )
      .addTo(instance);
  });
  instance.on("mouseenter", hitLayers, () => {
    instance.getCanvas().style.cursor = "pointer";
  });
  instance.on("mouseleave", hitLayers, () => {
    instance.getCanvas().style.cursor = "";
  });
}

/**
 * Point the camera at the data.
 *
 * Instantaneous, which is a fix rather than a preference: `fitBounds` with a duration is
 * an animation, and an animation that never runs leaves the camera on its default
 * centre, which is how this map once rendered nothing while holding 4,392 features.
 */
function frame(instance: MapLibreMap, payload: MapWindow) {
  if (payload.cells.length === 0) return;

  let west = Infinity;
  let east = -Infinity;
  let south = Infinity;
  let north = -Infinity;
  for (const cell of payload.cells) {
    if (cell.longitude < west) west = cell.longitude;
    if (cell.longitude > east) east = cell.longitude;
    if (cell.latitude < south) south = cell.latitude;
    if (cell.latitude > north) north = cell.latitude;
  }

  if (west === east && south === north) {
    instance.jumpTo({ center: [west, south], zoom: 8 });
    return;
  }

  // Padding that scales with the window. A flat half-degree margin is generous for one
  // region and invisible across both of them.
  const pad = Math.max(0.4, (east - west) * 0.06, (north - south) * 0.06);
  instance.fitBounds(
    [
      [west - pad, south - pad],
      [east + pad, north + pad],
    ],
    { padding: 36, duration: 0, maxZoom: 9 },
  );
}

/**
 * Push data into the map and aim the camera at it. Returns a teardown.
 *
 * The container often has no size when the map loads — the stylesheet that gives the grid
 * its columns may not have applied yet — and fitting a zero-width box produces a
 * degenerate camera, which is the second way this map has rendered nothing while holding
 * thousands of features. Waiting on the map's own `idle` event does not help, because an
 * empty map never reaches idle. Watching the container means the fit happens the moment
 * there is a size to fit to, whenever that turns out to be.
 */
function paint(instance: MapLibreMap, payload: MapWindow): () => void {
  const source = instance.getSource("cells") as GeoJSONSource | undefined;
  const write = () => {
    const current = instance.getSource("cells") as GeoJSONSource | undefined;
    current?.setData(toFeatureCollection(payload.cells, payload.breaks) as never);
  };
  write();

  const teardown: Array<() => void> = [];
  if (payload.cells.length === 0) return () => teardown.forEach((stop) => stop());

  const container = instance.getContainer();
  if (container.clientWidth > 0) {
    frame(instance, payload);
  } else {
    const observer = new ResizeObserver(() => {
      if (container.clientWidth > 0) {
        observer.disconnect();
        frame(instance, payload);
      }
    });
    observer.observe(container);
    // Last resort if the element never gets a size at all (display:none, odd embedding).
    const timer = window.setTimeout(() => {
      observer.disconnect();
      if (container.clientWidth > 0) frame(instance, payload);
    }, 4000);
    teardown.push(() => {
      observer.disconnect();
      window.clearTimeout(timer);
    });
  }

  // A setData issued in the same tick as the source is created can be dropped before the
  // source has built its tiles, so the data goes in once more on the next frame.
  const raf = requestAnimationFrame(() => source && write());
  teardown.push(() => cancelAnimationFrame(raf));

  return () => teardown.forEach((stop) => stop());
}

export default function AnomalyMap({ initial }: { initial: MapWindow }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const teardown = useRef<(() => void) | null>(null);
  const [theme] = useTheme();

  const [layer, setLayer] = useState<LayerKey>("fire");
  const [period, setPeriod] = useState<PeriodValue>("iberia2025");
  const [region, setRegion] = useState<string>("iberia_fire");
  const [mode, setMode] = useState<"all" | "anomalies">("all");
  const [data, setData] = useState<MapWindow>(initial);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  // Null until the probe has run, so the first frame does not commit to a renderer.
  const [engine, setEngine] = useState<Engine>(null);
  // The map's `load` event fires *after* the first data effect has already run, so a
  // handler that built the source from a stale closure would paint an empty collection
  // and the first render would show nothing. The ref carries whatever is current into the
  // handler whenever it fires.
  const latest = useRef<MapWindow>(initial);
  // Written from an effect rather than in the render body. MapLibre's handlers outlive the
  // render that registered them, so they have to read current values through a ref, and a
  // ref write during render is a side effect React development builds warn about.
  const themeRef = useRef(theme);
  // Same reason: the layer handlers print whichever unit is selected when they are clicked,
  // not the one that happened to be current when the style loaded.
  const unitRef = useRef(unitFor(initial.layer));
  const styleApplied = useRef<string | null>(null);

  // Recomputed when the theme changes, so the map, the legend and the strip cannot end
  // up on different ends of the ramp.
  const ramp = useMemo(() => rampHex(), [theme]);
  const today = useMemo(() => new Date().toISOString().slice(0, 10), []);
  const selected = PERIODS.find((item) => item.value === period) ?? PERIODS[1];
  const unit = unitFor(data.layer);
  useEffect(() => {
    unitRef.current = unit;
  }, [unit]);
  useEffect(() => {
    themeRef.current = theme;
  }, [theme]);

  const load = useCallback(
    async (
      nextLayer: LayerKey,
      nextPeriod: PeriodValue,
      nextMode: "all" | "anomalies",
      nextRegion: string,
      signal: AbortSignal,
    ) => {
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
        const response = await fetch(`/api/map?${params}`, { signal });
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        const payload = (await response.json()) as MapWindow;
        if (signal.aborted) return;
        latest.current = payload;
        forgetBoundaries();
        setData(payload);
        setFailure(null);
      } catch (error) {
        // An aborted request is the expected consequence of the reader changing a control
        // again, not a failure worth reporting.
        if (signal.aborted) return;
        setFailure(error instanceof Error ? error.message : String(error));
      } finally {
        if (!signal.aborted) setLoading(false);
      }
    },
    [today],
  );

  // Changing any control refetches; the map instance is created once and never recreated.
  useEffect(() => {
    const controller = new AbortController();
    void load(layer, period, mode, region, controller.signal);
    return () => controller.abort();
  }, [layer, period, mode, region, load]);

  useEffect(() => {
    if (!container.current || map.current) return;

    if (!webglUsable()) {
      setEngine("canvas");
      return;
    }

    let instance: MapLibreMap;
    try {
      styleApplied.current = BASEMAP[themeRef.current];
      instance = new maplibregl.Map({
        container: container.current,
        style: BASEMAP[themeRef.current],
        center: [-3, 42],
        zoom: 6,
        attributionControl: { compact: true },
      });
    } catch (error) {
      // MapLibre's constructor is where the WebGL failure surfaces, so this is the only
      // place the fallback can be chosen from a real failure rather than a prediction.
      setEngine("canvas");
      setFailure(error instanceof Error ? error.message : String(error));
      return;
    }

    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    instance.addControl(new maplibregl.ScaleControl({ maxWidth: 90 }), "bottom-left");

    // A basemap URL is given up on once, not on every retry of the same one.
    let failedStyle: string | null = null;
    let styleReady = false;

    const reattach = () => {
      // `style.load` rather than `isStyleLoaded()`. The latter asks whether the basemap's
      // *tiles* have all arrived, which a streaming vector style can keep false
      // indefinitely — gating the data layers on it left the map holding 4,392 features
      // and drawing none of them. What is needed here is the style document, and
      // `style.load` is the event that says it exists. It also covers the style swap a
      // theme change triggers, which is what takes the data layers away and gives them
      // back.
      if (!instance.getSource("cells")) {
        attachData(instance, latest.current, () => unitRef.current, themeRef.current);
        teardown.current?.();
        teardown.current = paint(instance, latest.current);
      }
    };

    const onStyleReady = () => {
      styleReady = true;
      reattach();
    };

    instance.on("error", () => {
      // Tile-level errors are the basemap's business and are not fatal. A failure to fetch
      // the style document is, because without one the data layers have nothing to attach
      // to. Swapping in a local background keeps the cells on screen.
      if (styleReady) return;
      const wanted = styleApplied.current;
      if (!wanted || wanted === failedStyle) return;
      failedStyle = wanted;
      instance.setStyle(blankStyle(themeRef.current));
    });
    instance.on("style.load", onStyleReady);
    instance.on("load", onStyleReady);
    instance.on("webglcontextlost", () => setEngine("canvas"));

    map.current = instance;
    setEngine("webgl");
    // Exposed deliberately for diagnostics. A maplibre canvas that renders nothing is a
    // black box from the outside, and being able to inspect the source, the layer list and
    // the viewport from devtools is the difference between diagnosing that and guessing.
    (window as unknown as Record<string, unknown>).__terrasentinelMap = instance;

    return () => {
      teardown.current?.();
      teardown.current = null;
      map.current = null;
      delete (window as unknown as Record<string, unknown>).__terrasentinelMap;
      instance.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A theme change needs a different basemap. `setStyle` drops the data layers, so the
  // `style.load` handler puts them back.
  useEffect(() => {
    const instance = map.current;
    if (!instance || engine !== "webgl") return;
    if (instance.getLayer("ts-backdrop")) {
      // Already on the local style because the basemap would not load. Repaint it rather
      // than re-request a URL that has already failed.
      instance.setPaintProperty("ts-backdrop", "background-color", BACKDROP[theme]);
      return;
    }
    const wanted = BASEMAP[theme];
    if (styleApplied.current === wanted) return;
    styleApplied.current = wanted;
    instance.setStyle(wanted);
  }, [theme, engine]);

  // Paint on every data change. Framed here as well as on load, because a window change
  // should reframe rather than leave the camera where the last window put it.
  useEffect(() => {
    const instance = map.current;
    if (!instance || engine !== "webgl") return;
    teardown.current?.();
    teardown.current = paint(instance, data);
  }, [data, engine]);

  const hasCells = data.cells.length > 0;
  const hasScale = data.breaks.length >= 4;
  const densest = useMemo(() => data.cells.slice(0, 8), [data.cells]);
  const peak = data.peak;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-4 py-2.5">
        <Select
          value={period}
          onChange={(event) => setPeriod(event.target.value as PeriodValue)}
          aria-label="Period"
        >
          {PERIODS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </Select>
        <Select
          value={region}
          onChange={(event) => setRegion(event.target.value)}
          aria-label="Region"
        >
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
                  ? "only cells in months that contain a flagged day"
                  : "only cells on days the detector flagged",
            },
          ]}
          value={mode}
          onChange={setMode}
        />
        <div className="ml-auto flex items-center gap-3 text-xs text-[var(--color-muted)]">
          <Tabs
            items={[
              { value: "fire", label: LAYERS.fire, hint: "H3 res 7, about 5 km², daily" },
              { value: "sst", label: LAYERS.sst, hint: "H3 res 5, about 253 km², monthly" },
            ]}
            value={layer}
            onChange={setLayer}
            size="sm"
          />
        </div>
      </div>

      <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs text-[var(--color-muted)]">
        <li>{selected.hint}</li>
        <li className="tabular-nums">
          {formatNumber(data.totalCells)} cells
          {data.truncated && ` (showing the densest ${formatNumber(data.cells.length)})`}
        </li>
        <li className="tabular-nums">
          {data.from} to {data.to}
        </li>
        <li>
          {data.grain === "month"
            ? "monthly resolution, each cell stamped with the first of its month"
            : "daily resolution"}
        </li>
        {peak && (
          <li className="tabular-nums">
            densest cell {formatNumber(peak.value)} in {peak.region_id}
          </li>
        )}
        {engine === "canvas" && (
          <li>
            drawn without WebGL, so the basemap and the zoom controls are missing; every
            value is still here
          </li>
        )}
      </ul>

      <div className="relative" aria-busy={loading}>
        {engine === "canvas" ? (
          <div className="h-[clamp(320px,54vh,560px)] w-full bg-[var(--color-background)]">
            <HexCanvas data={data} layer={data.layer} theme={theme} />
          </div>
        ) : (
          <div
            ref={container}
            className="h-[clamp(320px,54vh,560px)] w-full bg-[var(--color-background)]"
          />
        )}

        {loading && (
          <p className="pointer-events-none absolute left-3 top-3 rounded bg-[var(--color-surface)] px-2 py-1 text-xs text-[var(--color-muted)]">
            loading…
          </p>
        )}

        {failure && (
          <div className="absolute inset-x-6 top-10">
            <Empty title="This window did not load">
              <p>
                The map is still showing the previous window. The request failed: {failure}.
              </p>
            </Empty>
          </div>
        )}

        {!hasCells && !loading && !failure && (
          <div className="absolute inset-x-6 top-6">
            <Empty title="No cells in this window">
              <p>
                {mode === "anomalies"
                  ? data.grain === "month"
                    ? "No month in this range has a flagged day. Try “All cells”, or a period anchored to a documented event such as August 2025."
                    : "No day in this range was flagged. Try “All cells”, or a period anchored to a documented event such as August 2025."
                  : "This window has no data. The Sentinel layers are absent too, until the Earth Engine backfill runs."}
              </p>
              {data.coverage.from !== "" &&
                (data.to < data.coverage.from || data.from > data.coverage.to) && (
                  <p className="mt-2">
                    The requested window ({data.from} to {data.to}) falls outside this
                    layer's coverage, {data.coverage.from} to {data.coverage.to}
                    {data.grain === "month"
                      ? " — the SST mart is monthly and its backfill is still filling in earlier periods."
                      : "."}
                  </p>
                )}
            </Empty>
          </div>
        )}
      </div>

      {/* The densest cells in text. Both renderers are canvases, so a screen reader
          otherwise reaches this map only through the controls above it. */}
      {hasCells && (
        <p className="sr-only">
          Densest cells:{" "}
          {densest
            .map((cell) => `${formatNumber(cell.value)} ${unit} in ${cell.region_id}`)
            .join("; ")}
          .
        </p>
      )}

      {hasCells && hasScale && (
        <Legend breaks={data.breaks} domain={data.domain} layer={layer} grain={data.grain} ramp={ramp} />
      )}
      <ActivityStrip activity={data.activity} grain={data.grain} ramp={ramp} />
    </div>
  );
}

/** The legend prints the real break values, so the colour scale can be checked rather
 *  than taken on trust. It also states the resolution the colours are drawn on, and the
 *  grain, because the grain changes what a value means. */
function Legend({
  breaks,
  domain,
  layer,
  grain,
  ramp,
}: {
  breaks: number[];
  domain: { min: number; max: number };
  layer: LayerKey;
  grain: MapWindow["grain"];
  ramp: string[];
}) {
  const low = formatNumber(breaks[0]);
  const high = formatNumber(breaks[3]);

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-[var(--color-border)] px-4 py-2.5 text-xs">
      <span className="text-[var(--color-muted)]">
        {layer === "fire" ? "Detections per cell" : "Monthly SST anomaly per cell"}
        <span className="ml-1 text-[var(--color-subtle)]">
          (quantiles of what is shown{grain === "month" ? ", monthly means" : ""})
        </span>
      </span>
      <div className="flex items-center gap-2">
        <div className="flex overflow-hidden rounded border border-[var(--color-border)]">
          {ramp.map((colour) => (
            <span key={colour} className="h-3 w-7" style={{ background: colour }} aria-hidden="true" />
          ))}
        </div>
        <span className="tabular-nums text-[var(--color-muted)]">
          under {low} … {high} and above
        </span>
      </div>
      <span className="tabular-nums text-[var(--color-muted)]">
        range {formatNumber(domain.min)}–{formatNumber(domain.max)}
      </span>
      <span className="ml-auto text-[var(--color-subtle)]">
        {layer === "fire" ? "H3 res 7, about 5 km² cells" : "H3 res 5, about 253 km² cells"}
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
 * Both the height and the shade come from the same number, the cell count. The height is
 * the channel the eye reads; the shade is there for the bars too short to tell apart. The
 * previous shade used each period's peak cell value against the map's quantile breaks,
 * which in any genuinely extreme week is the top step for every bar, so the whole strip
 * came out one flat colour.
 *
 * The label tracks the layer's grain: on the monthly SST layer each bar *is* a month, and
 * calling that a day would be a small lie the reader has no way to catch.
 */
function ActivityStrip({
  activity,
  grain,
  ramp,
}: {
  activity: MapWindow["activity"];
  grain: MapWindow["grain"];
  ramp: string[];
}) {
  const unit = grain === "month" ? "month" : "day";

  // Reduced rather than spread: `Math.max(...rows)` blows the argument limit on a window
  // long enough to hold tens of thousands of periods, and the full record nearly does.
  const summary = activity.reduce(
    (best, row) => (row.cells > best.cells ? row : best),
    { period_start: "", cells: 0, peak: 0 },
  );

  if (activity.length < 2 || summary.cells === 0) return null;

  return (
    <div className="border-t border-[var(--color-border)] px-4 py-3">
      <div className="mb-1.5 flex items-baseline justify-between gap-3 text-xs">
        <span className="text-[var(--color-muted)]">Cells per {unit}</span>
        <span className="tabular-nums text-[var(--color-subtle)]">
          busiest {summary.period_start}: {formatNumber(summary.cells)} cells, densest cell{" "}
          {formatNumber(summary.peak)}
        </span>
      </div>
      <div
        className="flex h-12 items-end gap-px"
        style={{ maxWidth: `${Math.max(activity.length * 14, 220)}px` }}
        role="img"
        aria-label={`Cells per ${unit} across ${activity.length} ${unit}s; busiest ${summary.period_start} with ${formatNumber(summary.cells)} cells`}
      >
        {activity.map((row) => {
          const share = row.cells / summary.cells;
          // Step 1 rather than 0: the bottom of the ramp is too dark to read as a bar
          // against this background, and a bar you cannot see carries nothing.
          const step = Math.min(ramp.length - 1, Math.max(1, Math.ceil(share * ramp.length) - 1));
          return (
            <span
              key={row.period_start}
              title={`${row.period_start}: ${formatNumber(row.cells)} cells, densest cell ${formatNumber(row.peak)}`}
              className="flex-1 rounded-t-sm"
              style={{ height: `${Math.max(3, Math.round(share * 100))}%`, background: ramp[step] }}
            />
          );
        })}
      </div>
    </div>
  );
}
