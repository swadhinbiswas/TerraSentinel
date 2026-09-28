import { afterEach, describe, expect, it, vi } from "vitest";
import { webglUsable } from "./webgl";

/**
 * The WebGL probe.
 *
 * This function is the only thing standing between a browser that cannot give MapLibre a
 * context and a blank map card. The failure it exists for is one where
 * `"WebGL2RenderingContext" in window` is still `true`, so the test has to stub
 * `getContext` rather than the global — a test that deleted the global would pass against
 * an implementation that never checked anything.
 */

const real = HTMLCanvasElement.prototype.getContext;

afterEach(() => {
  HTMLCanvasElement.prototype.getContext = real;
  vi.restoreAllMocks();
});

/** A context stub good enough to look real. */
function context(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    VERSION: 0x1f02,
    getParameter: vi.fn(() => "WebGL 2.0"),
    getExtension: vi.fn(() => null),
    ...overrides,
  } as unknown as RenderingContext;
}

describe("webglUsable", () => {
  it("is true when a context is handed out and answers a call", () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() => context()) as never;
    expect(webglUsable()).toBe(true);
  });

  it("falls back to webgl1 when webgl2 is refused", () => {
    // Plenty of machines only have webgl1. MapLibre tries webgl2 then webgl, and so must
    // this, or the probe would reject a browser the map would have worked in.
    const getContext = vi.fn((kind: string) => (kind === "webgl2" ? null : context()));
    HTMLCanvasElement.prototype.getContext = getContext as never;
    expect(webglUsable()).toBe(true);
    expect(getContext).toHaveBeenCalledWith("webgl2");
  });

  it("is false when getContext returns null, which is the reported failure", () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never;
    expect(webglUsable()).toBe(false);
  });

  it("is false when getContext throws, as some hardened browsers do", () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() => {
      throw new Error("blocked by policy");
    }) as never;
    expect(webglUsable()).toBe(false);
  });

  it("is false when a context is created but cannot answer a call", () => {
    // A blocklisted driver can hand out a context whose every call fails. Creating it is
    // not evidence that it works.
    HTMLCanvasElement.prototype.getContext = vi.fn(() =>
      context({ getParameter: vi.fn(() => null) }),
    ) as never;
    expect(webglUsable()).toBe(false);
  });

  it("is false when getParameter throws", () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() =>
      context({
        getParameter: vi.fn(() => {
          throw new Error("context lost");
        }),
      }),
    ) as never;
    expect(webglUsable()).toBe(false);
  });

  it("releases the probe's context slot", () => {
    // Browsers cap live contexts, and the real map needs one.
    const lose = vi.fn();
    const getExtension = vi.fn(() => ({ loseContext: lose }));
    HTMLCanvasElement.prototype.getContext = vi.fn(() => context({ getExtension })) as never;
    expect(webglUsable()).toBe(true);
    expect(getExtension).toHaveBeenCalledWith("WEBGL_lose_context");
    expect(lose).toHaveBeenCalledOnce();
  });

  it("does not treat a missing lose-context extension as a failure", () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() =>
      context({ getExtension: vi.fn(() => null) }),
    ) as never;
    expect(webglUsable()).toBe(true);
  });
});
