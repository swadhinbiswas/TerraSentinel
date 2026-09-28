import { describe, expect, it } from "vitest";
import { compactNumber, duration, relativeTime, STATE_META, wireClass } from "./meta";

/**
 * Presentation for the pipeline view.
 *
 * The formatting here is not cosmetic. `relativeTime` is what tells a reader whether a
 * stage is moving, and `duration` had to stop printing `0.0s` for a run that genuinely
 * recorded a sub-second measurement, because that reads as a broken instrument.
 */

const NOW = Date.parse("2026-09-28T12:00:00Z");
const ago = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();

describe("relativeTime", () => {
  it("says 'never' for a stage that has no run at all", () => {
    expect(relativeTime(null, NOW)).toBe("never");
  });

  it("picks a unit that suits the distance", () => {
    expect(relativeTime(ago(5), NOW)).toBe("just now");
    expect(relativeTime(ago(45), NOW)).toBe("45 seconds ago");
    expect(relativeTime(ago(60), NOW)).toBe("1 minute ago");
    expect(relativeTime(ago(3 * 3600), NOW)).toBe("3 hours ago");
    expect(relativeTime(ago(2 * 86400), NOW)).toBe("2 days ago");
    expect(relativeTime(ago(70 * 86400), NOW)).toBe("2 months ago");
    expect(relativeTime(ago(800 * 86400), NOW)).toBe("2 years ago");
  });

  it("calls the first few seconds 'just now'", () => {
    // Anything under ten seconds reads as the present, so "1 second ago" is deliberately
    // unreachable and the seconds plural is what carries the grammar.
    expect(relativeTime(ago(1), NOW)).toBe("just now");
    expect(relativeTime(ago(9), NOW)).toBe("just now");
    expect(relativeTime(ago(30), NOW)).toBe("30 seconds ago");
  });

  it("agrees in number and in grammar", () => {
    expect(relativeTime(ago(60), NOW)).toBe("1 minute ago");
    expect(relativeTime(ago(120), NOW)).toBe("2 minutes ago");
    expect(relativeTime(ago(3600), NOW)).toBe("1 hour ago");
    expect(relativeTime(ago(2 * 3600), NOW)).toBe("2 hours ago");
  });

  it("does not go negative if a clock is slightly off", () => {
    // The server's clock and the browser's disagree by a second now and then, and "-3
    // seconds ago" is worse than "just now".
    expect(relativeTime(ago(-5), NOW)).toBe("just now");
  });

  it("survives an unparseable timestamp", () => {
    expect(relativeTime("not a date", NOW)).toBe("unknown");
    expect(relativeTime("", NOW)).toBe("unknown");
  });
});

describe("duration", () => {
  it("reports a missing measurement as unknown", () => {
    expect(duration(null)).toBe("—");
  });

  it("does not present a sub-second measurement as zero", () => {
    // `pipeline_runs.duration_s` measures the interval the recorder observed, so the
    // transform's gold-sync records 0.0. "0.0s" reads as a broken measurement; "<1s" is
    // the same fact without inviting that conclusion.
    expect(duration(0)).toBe("<1s");
    expect(duration(0.4)).toBe("<1s");
  });

  it("keeps a decimal only on a short run", () => {
    // 9.6s is worth reading as 9.6; 12.3s is not, and the trailing digit is noise.
    expect(duration(9.6)).toBe("9.6s");
    expect(duration(12.3)).toBe("12s");
    expect(duration(45)).toBe("45s");
    expect(duration(90)).toBe("1m 30s");
    expect(duration(3720)).toBe("1h 2m");
  });
});

describe("compactNumber", () => {
  it("abbreviates only above a thousand", () => {
    expect(compactNumber(999)).toBe("999");
    expect(compactNumber(1500)).toBe("1.5k");
    expect(compactNumber(80000)).toBe("80k");
    expect(compactNumber(2_400_000)).toBe("2.4M");
  });

  it("reports a missing count as unknown", () => {
    expect(compactNumber(null)).toBe("—");
    expect(compactNumber(undefined)).toBe("—");
  });
});

describe("STATE_META", () => {
  it("covers every state the report can produce", () => {
    // A state with no presentation would render an unlabelled dot, which is the one thing
    // this page must never do.
    for (const state of ["live", "overdue", "failed", "unrecorded", "external"] as const) {
      const meta = STATE_META[state];
      expect(meta).toBeDefined();
      expect(meta.label.length).toBeGreaterThan(0);
      expect(meta.dot).toMatch(/bg-\[var\(--color-/);
      expect(meta.text).toMatch(/text-\[var\(--color-/);
    }
  });

  it("gives the two healthy-looking states a different treatment from the two bad ones", () => {
    expect(STATE_META.live.border).not.toBe(STATE_META.overdue.border);
    expect(STATE_META.overdue.border).not.toBe(STATE_META.failed.border);
  });
});

describe("wireClass", () => {
  it("animates only a live stage", () => {
    expect(wireClass("live", false)).toBe("flow-wire");
    expect(wireClass("live", true)).toBe("flow-wire-v");
  });

  it("stalls a broken stage rather than animating it", () => {
    // A moving wire next to a failed stage is the most misleading thing this page could do.
    for (const state of ["overdue", "failed"] as const) {
      expect(wireClass(state, false)).toContain("stalled");
      expect(wireClass(state, true)).toContain("stalled");
    }
  });

  it("uses the vertical variant for the stacked layout", () => {
    // The animated sweep runs along the wire's axis, so a horizontal gradient on a 1px
    // column is a flat colour and the connector disappears.
    expect(wireClass("unrecorded", true)).toBe("flow-wire-v-idle");
    expect(wireClass("unrecorded", false)).toBe("flow-wire-idle");
  });
});
