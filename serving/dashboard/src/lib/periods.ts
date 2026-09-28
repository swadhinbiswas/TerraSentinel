/** The layers this map can show. Mirrors `LAYERS` in the component that renders the pickers. */
export type LayerKey = "fire" | "sst";

/**
 * The window presets, as URL-safe values.
 *
 * Written out rather than derived from `PERIODS`: the array is annotated with
 * `PeriodPreset`, which needs this type, so deriving it from the array would be circular.
 * The `satisfies` below is what keeps the two from drifting — a preset whose value is not
 * in this union fails to type-check.
 */
export type PeriodValue =
  | "recent"
  | "iberia2025"
  | "winter2026"
  | "season2026"
  | "all";

/**
 * The map's window presets.
 *
 * Data rather than JSX, in its own module, for two reasons. The component that uses them
 * is over a thousand lines and a list of dates is the one thing in it that is worth
 * testing on its own. And the layer filter below is a table, not a branch, so adding a
 * window is adding one object and not editing a conditional.
 *
 * Every window is anchored to something measured — a documented event, a season, or the
 * end of the archive — rather than to round calendar months, because a window chosen for
 * being tidy is a window with nothing in it.
 */

/** One window preset. The hint takes the layer because a shared window cannot carry one
 *  layer's description — see `season2026`. */
export interface PeriodPreset {
  value: PeriodValue;
  label: string;
  from: (today: string) => string;
  to: (today: string) => string;
  hint: (layer: LayerKey) => string;
}

/** Presets anchored to measured events rather than to calendar windows. */
export const PERIODS = [
  {
    value: "recent",
    label: "Last 30 days",
    from: (today: string) => shiftDays(today, -30),
    to: (today: string) => today,
    hint: (layer: LayerKey) =>
      layer === "sst" ? "the most recent months on record" : "the end of the season, which is quiet",
  },
  {
    value: "iberia2025",
    label: "Aug 2025 megafire",
    from: () => "2025-08-13",
    to: () => "2025-08-19",
    hint: () => "13,329 detections on the peak day",
  },
  {
    value: "winter2026",
    label: "Feb 2026 anomaly",
    from: () => "2026-02-22",
    to: () => "2026-02-28",
    hint: () => "1,184 detections against a winter median of 66",
  },
  {
    value: "season2026",
    label: "2026 season",
    from: () => "2026-06-01",
    to: () => "2026-09-20",
    // Layer-aware. A window that both layers share cannot carry one layer's description:
    // "the whole fire season" shown against a sea-surface-temperature map is the same
    // category of mistake as offering the window at all, one sentence later.
    hint: (layer: LayerKey) =>
      layer === "sst"
        ? "every month the SST mart has"
        : "the whole fire season",
  },
  {
    value: "all",
    label: "Full record",
    from: () => "2024-09-01",
    to: () => "2030-01-01",
    hint: () => "every cell in the archive",
  },
] satisfies readonly PeriodPreset[];

/**
 * Which layers a window preset is meaningful for. Absent means every layer.
 *
 * The two dated fire events are scoped to `fire` because `gold_h3_sst` has no rows before
 * June 2026 — it is a monthly mart whose backfill is still filling in earlier periods.
 * Offering it anyway produced an empty map, and the empty state then explained the gap by
 * naming the Earth Engine backfill, which has nothing to do with sea-surface temperature.
 * One dropdown, three wrong claims.
 */
export const PERIOD_LAYERS: Partial<Record<PeriodValue, LayerKey[]>> = {
  recent: ["fire", "sst"],
  iberia2025: ["fire"],
  winter2026: ["fire"],
  season2026: ["fire", "sst"],
  all: ["fire", "sst"],
};

/**
 * The presets that make sense for the layer currently selected.
 *
 * Never empty for any layer, because an empty `<select>` renders as a blank control and
 * the map would be stuck on whatever it was showing with no way to change it. A layer only
 * loses a preset, never all of them.
 */
export function periodsFor(layer: LayerKey): readonly PeriodValue[] {
  return PERIODS.filter((preset) => {
    const layers = PERIOD_LAYERS[preset.value];
    return !layers || layers.includes(layer);
  }).map((preset) => preset.value);
}

/** The full preset record, with its hint resolved for the layer being shown. */
export function periodFor(value: PeriodValue, layer: LayerKey) {
  const preset = PERIODS.find((candidate) => candidate.value === value) ?? PERIODS[0];
  return { ...preset, hint: preset.hint(layer) };
}

export function shiftDays(iso: string, days: number): string {
  const then = new Date(`${iso}T00:00:00Z`);
  then.setUTCDate(then.getUTCDate() + days);
  return then.toISOString().slice(0, 10);
}
