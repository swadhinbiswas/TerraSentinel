import { describe, expect, it } from "vitest";
import { formatNumber, rampHex, rampStep } from "./utils";

/**
 * `rampHex` converts the CSS intensity tokens into hex for the two canvas renderers.
 *
 * The conversion is the reason this file exists. It is a hand-written oklch→sRGB
 * transform, and when it was wrong the symptom was a map whose top step was invisible on
 * the light theme — which no type checker and no visual review of the code would have
 * caught. The expected values below are computed from the token definitions in
 * `styles/global.css`, independently of the implementation.
 */

/** The tokens, transcribed from the `@theme` and `[data-theme="light"]` blocks. */
const DARK: [number, number, number][] = [
  [0.44, 0.06, 265],
  [0.62, 0.11, 55],
  [0.72, 0.15, 62],
  [0.8, 0.17, 78],
  [0.93, 0.11, 95],
];
const LIGHT: [number, number, number][] = [
  [0.82, 0.03, 265],
  [0.74, 0.13, 55],
  [0.66, 0.16, 52],
  [0.55, 0.18, 42],
  [0.42, 0.17, 28],
];

/** An independent oklch→sRGB reference, written from the spec rather than shared. */
function reference([L, C, H]: [number, number, number]): string {
  const h = (H * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.291485548 * b;
  const l = l_ ** 3;
  const m = m_ ** 3;
  const s = s_ ** 3;
  const lin = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  return lin
    .map((u) => {
      const c = Math.max(0, Math.min(1, u));
      const e = c > 0.0031308 ? 1.055 * c ** (1 / 2.4) - 0.055 : 12.92 * c;
      return Math.round(e * 255)
        .toString(16)
        .padStart(2, "0");
    })
    .join("");
}

function setThemeTokens(tokens: [number, number, number][]) {
  const root = document.documentElement;
  root.removeAttribute("style");
  // Tailwind v4 emits these as percentages; the parser has to handle that form.
  tokens.forEach(([l, c, h], index) => {
    root.style.setProperty(`--color-i${index + 1}`, `oklch(${Math.round(l * 100)}% ${c} ${h})`);
  });
}

describe("rampHex", () => {
  it("converts the dark theme tokens", () => {
    setThemeTokens(DARK);
    const ramp = rampHex();
    expect(ramp).toHaveLength(5);
    DARK.forEach((token, index) => {
      expect(ramp[index]).toBe(`#${reference(token)}`);
    });
  });

  it("converts the light theme tokens", () => {
    // The whole point: the light ramp is light-to-dark, and reading the dark tokens here
    // is what made the top step invisible on a white page.
    setThemeTokens(LIGHT);
    const ramp = rampHex();
    LIGHT.forEach((token, index) => {
      expect(ramp[index]).toBe(`#${reference(token)}`);
    });
    expect(ramp[0]).not.toBe(ramp[4]);
  });

  it("produces six-digit hex in range", () => {
    setThemeTokens(DARK);
    for (const colour of rampHex()) {
      expect(colour).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it("falls back wholesale rather than mixing converted values with defaults", () => {
    // A partially-read ramp would mis-colour part of the map, which is worse than a
    // slightly-off whole-map ramp, so one unreadable token discards all five.
    const root = document.documentElement;
    root.removeAttribute("style");
    root.style.setProperty("--color-i1", "oklch(44% .06 265)");
    root.style.setProperty("--color-i2", "not a colour");
    const ramp = rampHex();
    expect(ramp).toHaveLength(5);
    expect(ramp.every((c) => c.startsWith("#"))).toBe(true);
  });

  it("survives the tokens being absent entirely", () => {
    document.documentElement.removeAttribute("style");
    expect(rampHex()).toHaveLength(5);
  });

  it("does not return a ramp whose last step is invisible on the page background", () => {
    // Relative luminance, so the contrast claim is measured rather than asserted.
    const luminance = (hex: string) => {
      const channel = (value: number) => {
        const v = value / 255;
        return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      };
      const r = channel(parseInt(hex.slice(1, 3), 16));
      const g = channel(parseInt(hex.slice(3, 5), 16));
      const b = channel(parseInt(hex.slice(5, 7), 16));
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    for (const [theme, background] of [
      [DARK, 0.003],
      [LIGHT, 0.955],
    ] as const) {
      setThemeTokens(theme as [number, number, number][]);
      const ramp = rampHex();
      for (const step of ramp) {
        expect(Math.abs(luminance(step) - background)).toBeGreaterThan(0.05);
      }
    }
  });
});

describe("rampStep", () => {
  const breaks = [5, 78, 200, 1000];

  it("maps a value below the first break to step 0", () => {
    expect(rampStep(0, breaks)).toBe(0);
    expect(rampStep(4.9, breaks)).toBe(0);
  });

  it("counts the boundaries a value reaches", () => {
    expect(rampStep(5, breaks)).toBe(1);
    expect(rampStep(78, breaks)).toBe(2);
    expect(rampStep(200, breaks)).toBe(3);
    expect(rampStep(1000, breaks)).toBe(4);
  });

  it("clamps above the top break", () => {
    expect(rampStep(99999, breaks)).toBe(4);
  });

  it("treats an empty break list as a single step rather than dividing by zero", () => {
    // `EMPTY_MAP_WINDOW.breaks` is `[]`, and a NaN step would index the ramp as undefined.
    expect(rampStep(42, [])).toBe(0);
  });

  it("handles a negative value, which SST anomalies can produce", () => {
    expect(rampStep(-1.5, breaks)).toBe(0);
  });
});

describe("formatNumber", () => {
  it("renders an em dash for anything that is not a number", () => {
    expect(formatNumber(null)).toBe("—");
    expect(formatNumber(undefined)).toBe("—");
    expect(formatNumber(Number.NaN)).toBe("—");
  });

  it("pads to the requested digits", () => {
    expect(formatNumber(1.5, 2)).toMatch(/1\.50$/);
    expect(formatNumber(3, 0)).toMatch(/3$/);
  });
});
