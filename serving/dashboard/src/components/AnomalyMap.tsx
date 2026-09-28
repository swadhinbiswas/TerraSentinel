import type {
  ExpressionSpecification,
  GeoJSONSource,
  Map as MapLibreMap,
  StyleSpecification,
} from "maplibre-gl";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import type { MapWindow } from "@/lib/queries";
import {
  PERIODS,
  periodFor,
  periodsFor,
  type LayerKey,
  type PeriodValue,
} from "@/lib/periods";
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

/** Scoping to one region is what makes a window legible: spanning both study regions puts
 *  the camera 38 degrees wide, where 1 km cells are sub-pixel. */
const REGIONS = [
  { value: "iberia_fire", label: "Iberia" },
  { value: "greece_fire", label: "Greece" },
  { value: "all", label: "Both regions" },
] as const;

const LAYERS: Record<LayerKey, string> = { fire: "Fire detections", sst: "SST anomaly" };
const LAYER_KEYS = Object.keys(LAYERS) as LayerKey[];
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
function rampExpression(ramp: string[]): ExpressionSpecification {
  return ["match", ["get", "step"], 0, ramp[0], 1, ramp[1], 2, ramp[2], 3, ramp[3], ramp[4]];
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
  // MapLibre is loaded dynamically, after the WebGL probe, so nothing at module scope can
  // close over it. The one class `attachData` needs is passed in rather than imported.
  Popup: typeof import("maplibre-gl").Popup,
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
    new Popup({ closeButton: true, maxWidth: "260px" })
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
function frame(instance: MapLibreMap, payload: MapWindow | null) {
  if (!payload || payload.cells.length === 0) return;

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
function paint(instance: MapLibreMap, payload: MapWindow | null): () => void {
  const source = instance.getSource("cells") as GeoJSONSource | undefined;
  const teardown: Array<() => void> = [];
  if (!payload || payload.cells.length === 0) return () => teardown.forEach((stop) => stop());

  const write = () => {
    const current = instance.getSource("cells") as GeoJSONSource | undefined;
    if (!current) return;
    current.setData(toFeatureCollection(payload.cells, payload.breaks) as never);
  };
  write();

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

/**
 * Read the map's four choices from the query string, and write them back as they change.
 *
 * A permalink is only worth having if it survives a reload, so the query string is the
 * source of truth on mount and every change is reflected into it. `replaceState` rather
 * than `pushState`: each keystroke on a select should not become a history entry, but the
 * back button must still leave the page and must not walk through sixty window changes.
 *
 * Values are validated against the option lists rather than trusted, because the query
 * string is user-editable and a bad `layer=foo` would otherwise reach the API and come
 * back as a silently wrong response. An unrecognised value falls back to the default,
 * which is what a reader would have got by not editing it.
 */
function useUrlState<T extends string>(param: string, options: readonly T[], fallback: T) {
  const read = useCallback(() => {
    const raw = new URL(window.location.href).searchParams.get(param);
    return raw !== null && (options as readonly string[]).includes(raw) ? (raw as T) : fallback;
  }, [param, options, fallback]);

  const [value, setValue] = useState<T>(read);

  useEffect(() => {
    // Only once the reader has actually chosen something. Writing the defaults on mount
    // would rewrite a plain visit to `/` into a four-parameter URL nobody linked to, which
    // pollutes the referrer for the next page and makes the address bar lie about having
    // been shared.
    if (value === fallback) return;
    const url = new URL(window.location.href);
    if (url.searchParams.get(param) === value) return;
    url.searchParams.set(param, value);
    // The rest of the query is someone else's: this is an inbound link, and rewriting it
    // wholesale would drop whatever the sender meant to carry.
    window.history.replaceState(null, "", url);
  }, [param, value, fallback]);

  return [value, setValue] as const;
}

export default function AnomalyMap({ initial }: { initial?: MapWindow }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  // The repaint handle: what the last `paint()` registered, so the next one can undo it.
  // NOT the map's own teardown — the repaint effect calls this on every data change, so
  // sharing the slot with `instance.remove()` would destroy the map the first time a
  // window loaded. The two live separately.
  const teardown = useRef<(() => void) | null>(null);
  // Set once the MapLibre instance exists, cleared on unmount.
  const destroy = useRef<(() => void) | null>(null);
  const [theme] = useTheme();

  // From the query string, so a window can be linked to and the back button leaves the
  // page rather than walking through every change made to it.
  const [layer, setLayer] = useUrlState<LayerKey>(
    "layer",
    LAYER_KEYS,
    "fire",
  );
  const [period, setPeriod] = useUrlState<PeriodValue>(
    "window",
    PERIODS.map((item) => item.value),
    "iberia2025",
  );
  const [region, setRegion] = useUrlState<string>(
    "region",
    REGIONS.map((item) => item.value),
    "iberia_fire",
  );
  const [mode, setMode] = useUrlState<"all" | "anomalies">(
    "cells",
    ["all", "anomalies"],
    "all",
  );
  // Null until the first response lands. `EMPTY` would render the "no cells in this
  // window" state during what is actually a load, which is a lie told for a few hundred
  // milliseconds on every page view.
  const [data, setData] = useState<MapWindow | null>(initial ?? null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  // Null until the probe has run, so the first frame does not commit to a renderer.
  const [engine, setEngine] = useState<Engine>(null);
  // The map's `load` event fires *after* the first data effect has already run, so a
  // handler that built the source from a stale closure would paint an empty collection
  // and the first render would show nothing. The ref carries whatever is current into the
  // handler whenever it fires.
  const latest = useRef<MapWindow | null>(initial ?? null);
  // Written from an effect rather than in the render body. MapLibre's handlers outlive the
  // render that registered them, so they have to read current values through a ref, and a
  // ref write during render is a side effect React development builds warn about.
  const themeRef = useRef(theme);
  // Same reason: the layer handlers print whichever unit is selected when they are clicked,
  // not the one that happened to be current when the style loaded.
  const unitRef = useRef(unitFor(initial?.layer ?? "fire"));
  const styleApplied = useRef<string | null>(null);

  // Recomputed when the theme changes, so the map, the legend and the strip cannot end
  // up on different ends of the ramp.
  const ramp = useMemo(() => rampHex(), [theme]);
  const today = useMemo(() => new Date().toISOString().slice(0, 10), []);
  useEffect(() => {
    unitRef.current = unitFor(data?.layer ?? layer);
  }, [data?.layer, layer]);
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
      const preset = periodFor(nextPeriod, nextLayer);
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

    // Loaded here, after the probe, and not at the top of the module. MapLibre is roughly
    // two thirds of this island's bundle and is unusable on exactly the browsers that reach
    // this branch, so a static import would have every one of them download 250 kB
    // gzipped of a renderer they will never construct. The dynamic import splits it into
    // its own chunk that only the machines that can run it ever fetch.
    let cancelled = false;
    let maplibregl: typeof import("maplibre-gl");

    const boot = async () => {
      try {
        // Loaded here, after the probe, rather than at the top of the module. MapLibre is
        // roughly two thirds of this island's bundle and is unusable on exactly the
        // browsers that reach this branch, so a static import had every one of them
        // download ~250 kB gzipped of a renderer they would never construct. The dynamic
        // import puts it in its own chunk that only machines which can run it fetch.
        maplibregl = await import("maplibre-gl");
      } catch (error) {
        // A chunk that will not load is the same situation as a context that will not be
        // issued, and the canvas renderer does not care which.
        if (cancelled) return;
        setEngine("canvas");
        setFailure(error instanceof Error ? error.message : String(error));
        return;
      }
      // The island can unmount, or the renderer can be chosen elsewhere, while that chunk
      // is in flight.
      if (cancelled || !container.current || map.current) return;

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
        const current = latest.current;
        if (current && !instance.getSource("cells")) {
          attachData(instance, current, () => unitRef.current, themeRef.current, maplibregl.Popup);
          teardown.current?.();
          teardown.current = paint(instance, current);
        }
      };

      const onStyleReady = () => {
        styleReady = true;
        reattach();
      };

      instance.on("error", () => {
        // Tile-level errors are the basemap's business and are not fatal. A failure to
        // fetch the style document is, because without one the data layers have nothing
        // to attach to. Swapping in a local background keeps the cells on screen.
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
      // black box from the outside, and being able to inspect the source, the layer list
      // and the viewport from devtools is the difference between diagnosing that and
      // guessing.
      (window as unknown as Record<string, unknown>).__terrasentinelMap = instance;

      // The map's own teardown, in its own slot. `teardown` is the repaint handle and is
      // invoked on every data change, so putting `instance.remove()` there would take the
      // map down the first time a window loaded.
      destroy.current = () => {
        instance.remove();
        map.current = null;
        delete (window as unknown as Record<string, unknown>).__terrasentinelMap;
      };
    };

    void boot();

    // The effect-level cleanup. Unmounting before the chunk lands must not leave an
    // orphaned MapLibre instance: the dynamic import resolves *after* this runs, so
    // without the flag the map would be constructed onto a detached container and never
    // torn down. Once `boot` has registered the map's own teardown into `teardown.current`,
    // this runs that too.
    return () => {
      cancelled = true;
      destroy.current?.();
      destroy.current = null;
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

  // Switching layers can strand the selected window: "Aug 2025 megafire" is a fire event
  // and means nothing to the SST mart, which starts in June 2026. A `<select>` whose value
  // is not among its own options renders blank, so the window has to move with the layer.
  const availablePeriods = periodsFor(layer);
  useEffect(() => {
    if (availablePeriods.includes(period)) return;
    setPeriod(availablePeriods[0]);
  }, [availablePeriods, period, setPeriod]);

  // Move the camera to a cell. Called on click and on focus, so tabbing through the densest
  // list walks the map — which is the point of making that list keyboard-reachable, and
  // also how the numbers are checked against the rendering without a pointer.
  const focusCell = useCallback(
    (cell: { longitude: number; latitude: number }) => {
      const instance = map.current;
      if (!instance || engine !== "webgl") return;
      instance.easeTo({ center: [cell.longitude, cell.latitude], duration: 350 });
    },
    [engine],
  );

  return (
    <div>
      <MapControls
        period={period}
        onPeriod={setPeriod}
        region={region}
        onRegion={setRegion}
        mode={mode}
        onMode={setMode}
        layer={layer}
        onLayer={setLayer}
        // The mode tab is phrased by grain, which is only known once a window has arrived.
        grain={data?.grain ?? "day"}
        periods={availablePeriods}
      />
      {data === null ? (
        <MapSkeleton />
      ) : (
        <MapView
          data={data}
          layer={layer}
          mode={mode}
          period={period}
          engine={engine}
          theme={theme}
          ramp={ramp}
          loading={loading}
          failure={failure}
          container={container}
          focusCell={focusCell}
        />
      )}
    </div>
  );
}

/** The control strip. Split out because it renders before any window does. */
function MapControls({
  period,
  onPeriod,
  region,
  onRegion,
  mode,
  onMode,
  layer,
  onLayer,
  grain,
  periods,
}: {
  period: PeriodValue;
  onPeriod: (value: PeriodValue) => void;
  region: string;
  onRegion: (value: string) => void;
  mode: "all" | "anomalies";
  onMode: (value: "all" | "anomalies") => void;
  layer: LayerKey;
  onLayer: (value: LayerKey) => void;
  grain: MapWindow["grain"];
  /** The windows this layer can serve. Passed in rather than recomputed so the dropdown
   *  and the effect that corrects a stranded selection read from one list. */
  periods: readonly PeriodValue[];
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-4 py-2.5">
      <Select value={period} onChange={(event) => onPeriod(event.target.value as PeriodValue)} aria-label="Period">
        {periods.map((value) => (
          <option key={value} value={value}>
            {periodFor(value, layer).label}
          </option>
        ))}
      </Select>
      <Select value={region} onChange={(event) => onRegion(event.target.value)} aria-label="Region">
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
            label: grain === "month" ? "Anomalous months" : "Anomalous days",
            hint:
              grain === "month"
                ? "only cells in months that contain a flagged day"
                : "only cells on days the detector flagged",
          },
        ]}
        value={mode}
        onChange={onMode}
      />
      <div className="ml-auto flex items-center gap-3 text-xs text-[var(--color-muted)]">
        <Tabs
          items={[
            { value: "fire", label: LAYERS.fire, hint: "H3 res 7, about 5 km², daily" },
            { value: "sst", label: LAYERS.sst, hint: "H3 res 5, about 253 km², monthly" },
          ]}
          value={layer}
          onChange={onLayer}
          size="sm"
        />
      </div>
    </div>
  );
}

/**
 * The first paint, before the island's own fetch has answered.
 *
 * A frame of the right size rather than the empty state, because "no cells in this window"
 * is a claim about the data and the data has not arrived yet. It is the one thing this
 * change costs: the map now appears a round trip later than it used to, in exchange for a
 * landing page that is 50KB instead of 1.7MB.
 */
function MapSkeleton() {
  return (
    <div
      className="h-[clamp(320px,54vh,560px)] w-full bg-[var(--color-background)]"
      aria-hidden="true"
    >
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-2">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-[var(--color-border)] border-t-[var(--color-accent)]" />
          <p className="text-xs text-[var(--color-subtle)]">loading the window</p>
        </div>
      </div>
    </div>
  );
}

/** Everything that reads the window, which is now guaranteed to have one. */
function MapView({
  data,
  layer,
  mode,
  period,
  engine,
  theme,
  ramp,
  loading,
  failure,
  container,
  focusCell,
}: {
  data: MapWindow;
  layer: LayerKey;
  mode: "all" | "anomalies";
  period: PeriodValue;
  engine: Engine;
  theme: Theme;
  ramp: string[];
  loading: boolean;
  failure: string | null;
  container: RefObject<HTMLDivElement>;
  /** Moves the camera, so the densest-cell list can be operated from the keyboard. */
  focusCell: (cell: { longitude: number; latitude: number }) => void;
}) {
  // Resolved for the layer, so a shared window does not describe the other layer's data.
  const selected = periodFor(period, layer);
  const unit = unitFor(data.layer);
  const hasCells = data.cells.length > 0;
  const hasScale = data.breaks.length >= 4;
  const densest = data.cells.slice(0, 8);
  const peak = data.peak;
  // Whether the window is simply outside what this layer holds. Proved from the response's
  // own coverage bounds rather than assumed, because "no data" has several causes and
  // naming the wrong one is worse than naming none.
  const outsideCoverage =
    data.coverage.from !== "" &&
    (data.to < data.coverage.from || data.from > data.coverage.to);
  // The Sentinel layers are the only ones gated on the Earth Engine backfill, so it is the
  // only layer for which that can be the reason.
  const sentinelBlocked = data.layer === "sentinel";

  return (
    <div>
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
              {/*
                One cause, stated once, and only a cause the response can actually prove.
                The previous version named the Earth Engine backfill on every layer, so an
                SST window with no SST rows was explained by a Sentinel backfill that has
                nothing to do with it — and a second paragraph then gave the real reason,
                contradicting the first. Two paragraphs disagreeing is worse than one.
              */}
              {outsideCoverage ? (
                <>
                  <p>
                    This layer holds {data.coverage.from} to {data.coverage.to}; the window
                    asked for {data.from} to {data.to}
                    {data.grain === "month"
                      ? ". The SST mart is monthly and its backfill is still filling in earlier periods."
                      : "."}
                  </p>
                  <p className="mt-2">
                    The period list is already limited to the windows this layer has.
                  </p>
                </>
              ) : mode === "anomalies" ? (
                <p>
                  {data.grain === "month"
                    ? "No month in this range contains a flagged day."
                    : "No day in this range was flagged."}{" "}
                  Try “All cells”, or a window anchored to a documented event.
                </p>
              ) : (
                <>
                  <p>This layer has no rows in {data.from} to {data.to}.</p>
                  {sentinelBlocked && (
                    <p className="mt-2">
                      The Sentinel layers are absent for the same reason: their Earth
                      Engine backfill has not run.
                    </p>
                  )}
                </>
              )}
            </Empty>
          </div>
        )}
      </div>

      {/* The densest cells in text. Both renderers are canvases, so a screen reader
          otherwise reaches this map only through the controls above it. */}
      {hasCells && (
        // The keyboard path to the data. The map is a canvas in both renderers, so nothing
        // drawn in it is focusable and the values are otherwise only reachable with a
        // pointer. Collapsed rather than removed: this is the accessible interface to the
        // same numbers the map shows, and a visible duplicate table would be a second
        // thing to keep in step with the first.
        <details className="border-t border-[var(--color-border)]">
          <summary className="cursor-pointer list-none px-4 py-2 text-[11px] text-[var(--color-subtle)] hover:text-[var(--color-muted)]">
            <span className="font-medium text-[var(--color-muted)]">
              {densest.length} densest cells
            </span>{" "}
            {/* States only what it does. An earlier version of this line also claimed
                arrow-key navigation, which nothing implemented — a capability claim the
                reader can check and find false is worse than no claim. Tab reaches every
                one of these, and focusing one moves the camera. */}
            <span className="ml-1">
              — tab through them and the map follows
              {engine === "canvas"
                ? "; the canvas renderer has no camera, so use the values above"
                : ""}
            </span>
          </summary>
          <ol className="border-t border-[var(--color-border)] px-4 py-2">
            {densest.map((cell) => (
              <li key={`${cell.h3_index}-${cell.period_start}`}>
                <button
                  type="button"
                  onClick={() => focusCell(cell)}
                  onFocus={() => focusCell(cell)}
                  className="w-full rounded px-1 py-1 text-left text-[11px] tabular-nums hover:bg-[var(--color-surface)] focus-visible:bg-[var(--color-surface)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-accent)]"
                >
                  <span className="font-medium text-[var(--color-foreground)]">
                    {formatNumber(cell.value)} {unit}
                  </span>{" "}
                  <span className="text-[var(--color-muted)]">
                    {cell.region_id.replace(/_/g, " ")}, {cell.period_start.slice(0, 10)}
                  </span>{" "}
                  <span className="text-[var(--color-subtle)]">
                    {cell.latitude.toFixed(2)}, {cell.longitude.toFixed(2)}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </details>
      )}

      {hasCells && hasScale && (
        <Legend breaks={data.breaks} domain={data.domain} layer={layer} grain={data.grain} ramp={ramp} />
      )}
      <ActivityStrip
        activity={data.activity}
        grain={data.grain}
        ramp={ramp}
        truncated={data.truncated}
        shown={data.cells.length}
      />
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
  truncated,
  shown,
}: {
  activity: MapWindow["activity"];
  grain: MapWindow["grain"];
  ramp: string[];
  /** Whether the cell cap dropped rows, and how many survived it. */
  truncated: boolean;
  shown: number;
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
      {truncated && (
        // The strip counts every cell in the window; the map above draws only the densest
        // `shown`. Saying so here is what stops a reader from reading a bar as "these are
        // the cells on the map" when some of them were never sent.
        <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--color-subtle)]">
          These bars count all {formatNumber(activity.reduce((sum, row) => sum + row.cells, 0))}{" "}
          cells in the window. The map draws the {formatNumber(shown)} densest of them, so
          the quietest cells of a busy day are not shown.
        </p>
      )}
    </div>
  );
}
