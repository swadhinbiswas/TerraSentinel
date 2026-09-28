import { describe, expect, it } from "vitest";
import { PERIODS, PERIOD_LAYERS, periodFor, periodsFor } from "./periods";

/**
 * Which window presets a layer is allowed to offer.
 *
 * This exists because the SST layer was being offered "Aug 2025 megafire" and "Feb 2026
 * anomaly" — fire events that `gold_h3_sst` has no data for, because it is a monthly mart
 * that begins in June 2026. The result was an empty map and a message blaming the Earth
 * Engine backfill for a sea-surface-temperature gap. Both the wrong window and the wrong
 * explanation were reachable from the page.
 *
 * The rules are data rather than branching code, so the failure mode is a table that
 * forgets a preset rather than a condition that quietly stops applying.
 */

describe("PERIODS", () => {
  it("has a unique value per preset, since the value is the URL parameter", () => {
    const values = PERIODS.map((preset) => preset.value);
    expect(new Set(values).size).toBe(values.length);
  });

  it("gives every preset a from that is not after its to", () => {
    for (const preset of PERIODS) {
      expect(preset.from("2026-09-28") <= preset.to("2026-09-28")).toBe(true);
    }
  });

  it("orders its presets so the general ones come first and the specific events after", () => {
    // A reader arriving at the map wants "recent", and someone reading about the August
    // 2025 megafire wants to find that window without reading five options.
    expect(PERIODS[0].value).toBe("recent");
    expect(PERIODS.at(-1)?.value).toBe("all");
  });
});

describe("PERIOD_LAYERS", () => {
  it("only names presets that exist", () => {
    // A typo here would silently fall back to "every layer", which is the bug this table
    // was written to prevent.
    const known = new Set<string>(PERIODS.map((preset) => preset.value));
    for (const preset of Object.keys(PERIOD_LAYERS)) {
      expect(known.has(preset)).toBe(true);
    }
  });

  it("never leaves a preset with an empty layer list", () => {
    // An empty list would hide the preset from every layer, including the one it exists
    // for — silently, because a dropdown that is one option shorter looks deliberate.
    for (const layers of Object.values(PERIOD_LAYERS)) {
      expect(layers?.length ?? 0).toBeGreaterThan(0);
    }
  });

  it("keeps the SST layer away from the fire-event windows", () => {
    // The specific regression. `gold_h3_sst` starts in June 2026, so the two dated fire
    // events cannot be served from it.
    for (const value of periodsFor("sst")) {
      expect(["iberia2025", "winter2026"]).not.toContain(value);
    }
  });

  it("leaves the fire layer with every window, since the fire marts cover all of them", () => {
    expect(periodsFor("fire")).toHaveLength(PERIODS.length);
  });
});

describe("periodsFor", () => {
  it("always returns at least one window, so the select is never empty", () => {
    // An empty select renders as a blank control, and the map would be stuck showing
    // whatever was there before with no way to change it.
    for (const layer of ["fire", "sst"] as const) {
      expect(periodsFor(layer).length).toBeGreaterThan(0);
    }
  });

  it("keeps the original order, so filtering does not reshuffle the list", () => {
    // Order is the whole reason the presets are ordered.
    const values = periodsFor("sst");
    const expected = PERIODS.filter((preset) => values.includes(preset.value)).map(
      (preset) => preset.value,
    );
    expect(values).toEqual(expected);
  });

  it("always includes the full record, which is the one window no coverage gap can empty", () => {
    for (const layer of ["fire", "sst"] as const) {
      expect(periodsFor(layer)).toContain("all");
    }
  });

  it("defaults to every layer for a preset that names none", () => {
    // The escape hatch, so a new preset is visible everywhere until someone has a reason
    // to scope it.
    const unscoped = PERIODS.find((preset) => !PERIOD_LAYERS[preset.value]);
    if (unscoped) expect(periodsFor("sst")).toContain(unscoped.value);
  });
});


describe("periodFor", () => {
  it("resolves the hint for the layer being shown", () => {
    // The same class of mistake as the window list itself, one sentence later: a hint
    // that says "the whole fire season" while a sea-surface-temperature map is on screen.
    expect(periodFor("season2026", "sst").hint).not.toMatch(/fire/i);
    expect(periodFor("season2026", "fire").hint).toMatch(/fire season/i);
    expect(periodFor("recent", "sst").hint).not.toMatch(/season/i);
  });

  it("keeps the label the same whatever the layer, because a window has one name", () => {
    expect(periodFor("iberia2025", "fire").label).toBe(periodFor("iberia2025", "sst").label);
  });

  it("falls back rather than throwing on a value that is not a preset", () => {
    // A permalink can carry anything. Landing on the first window is better than a crash.
    const fallback = periodFor("nonsense" as never, "fire");
    expect(PERIODS.map((preset) => preset.value)).toContain(fallback.value);
  });

  it("keeps a from that is not after its to, per layer", () => {
    for (const layer of ["fire", "sst"] as const) {
      for (const preset of PERIODS) {
        const resolved = periodFor(preset.value, layer);
        expect(resolved.from("2026-09-28") <= resolved.to("2026-09-28")).toBe(true);
      }
    }
  });
});
