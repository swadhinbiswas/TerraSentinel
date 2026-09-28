/**
 * Whether this browser will actually hand out a WebGL context.
 *
 * MapLibre needs one and has no 2D fallback. Its `Map` constructor calls
 * `canvas.getContext("webgl2")`, and when that returns null it throws
 * `Failed to initialize WebGL` with the `webglcontextcreationerror` event as the
 * message. In a browser with hardware acceleration off, on a blocklisted driver, or in a
 * VM whose GPU process cannot bind a sequence, the reported reason is
 * `GL_VENDOR = Disabled, GL_RENDERER = Disabled, BindToCurrentSequence failed`.
 *
 * The trap is that `"WebGL2RenderingContext" in window` is still true in exactly those
 * browsers. The interface is declared; the driver behind it is not. A feature check that
 * only looks at the global is what makes this look like a code bug rather than an
 * environment one, so the check below asks for a real context and tries a call on it.
 */
export function webglUsable(): boolean {
  if (typeof document === "undefined") return false;

  let context: WebGLRenderingContext | WebGL2RenderingContext | null = null;
  try {
    const canvas = document.createElement("canvas");
    context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
  } catch {
    // Some hardened or embedded browsers throw here instead of returning null.
    return false;
  }

  if (!context) return false;

  try {
    // A context can be created and still be inert. One call is enough to tell.
    if (!context.getParameter(context.VERSION)) return false;
  } catch {
    return false;
  } finally {
    // Hand the probe's context slot back. Browsers cap live contexts, and this page
    // keeps one for the real map.
    context.getExtension("WEBGL_lose_context")?.loseContext();
  }

  return true;
}
