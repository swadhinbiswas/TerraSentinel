import { useEffect, useMemo, useRef } from "react";
import type { MapWindow } from "@/lib/queries";
import type { Theme } from "@/lib/theme";
import { rampHex } from "@/lib/utils";
import {
  boundaryFor,
  forgetBoundaries,
  mercatorX,
  mercatorY,
  propsFor,
  readoutFor,
  unitFor,
} from "./hex";

/**
 * The map, drawn on a 2D canvas.
 *
 * This is the renderer for browsers that will not give MapLibre a WebGL context:
 * hardware acceleration off, a blocklisted driver, a VM or a headless session. The
 * error MapLibre throws in that case is unrecoverable from the outside — the `Map`
 * object never finishes constructing — so the alternative to a blank card is not a
 * nicer error message but a second renderer.
 *
 * What it gives up: no basemap, no zoom, no panning. The camera frames the window and
 * stays there. What it keeps: the real H3 outlines, the same quantile colour scale and
 * the same per-cell readout, so the map still answers the question it exists to answer.
 */

const PADDING = 18;
/** Below this on-screen size a hexagon stops reading as a shape, so it becomes a mark. */
const MARK_SIZE = 3.4;
/** How far from a cell's centre a pointer still counts as being on it. */
const HIT_RADIUS = 8;

interface Drawn {
  props: ReturnType<typeof propsFor>;
  ring: Float64Array;
  cx: number;
  cy: number;
}

const OUTLINE: Record<Theme, string> = {
  dark: "rgba(255,255,255,0.3)",
  light: "rgba(0,0,0,0.24)",
};

const FILL_ALPHA: Record<Theme, number> = { dark: 0.78, light: 0.72 };

export default function HexCanvas({
  data,
  layer,
  theme,
}: {
  data: MapWindow;
  layer: MapWindow["layer"];
  theme: Theme;
}) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  // Kept out of state on purpose: the hit test runs on every pointer move, and routing
  // 4,000 results through a re-render would make the map feel worse than it looks.
  const drawn = useRef<Drawn[]>([]);

  const ramp = useMemo(() => rampHex(), [theme]);
  const cells = data.cells;
  const breaks = data.breaks;
  const unit = unitFor(layer);

  // Cheap to recompute and the pointer handler needs the current list, so the draw
  // function writes it rather than a second effect.
  const paint = useMemo(
    () => (target: HTMLCanvasElement) => {
      const context = target.getContext("2d");
      if (!context) return;

      const box = target.getBoundingClientRect();
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      target.width = Math.max(1, Math.round(box.width * ratio));
      target.height = Math.max(1, Math.round(box.height * ratio));
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, box.width, box.height);

      drawn.current = [];
      if (cells.length === 0 || box.width === 0 || box.height === 0) return;

      const rings = cells.map((cell) => boundaryFor(cell.h3_index));

      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      const projected = rings.map((ring) => {
        const flat = new Float64Array(ring.length * 2);
        for (let i = 0; i < ring.length; i += 1) {
          const x = mercatorX(ring[i][0]);
          const y = mercatorY(ring[i][1]);
          flat[i * 2] = x;
          flat[i * 2 + 1] = y;
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
        return flat;
      });

      const width = Math.max(box.width - PADDING * 2, 1);
      const height = Math.max(box.height - PADDING * 2, 1);
      const spanX = Math.max(maxX - minX, 1e-9);
      const spanY = Math.max(maxY - minY, 1e-9);
      const scale = Math.min(width / spanX, height / spanY);
      const offsetX = PADDING + (width - spanX * scale) / 2;
      const offsetY = PADDING + (height - spanY * scale) / 2;
      const project = (x: number, y: number): [number, number] => [
        offsetX + (x - minX) * scale,
        offsetY + (y - minY) * scale,
      ];

      // Low values first, so the hottest cells end up on top rather than underneath the
      // tail. The rows arrive ordered by value descending, which is the opposite of what
      // the paint order wants.
      const order = cells
        .map((_, index) => index)
        .sort((a, b) => Number(cells[a].value) - Number(cells[b].value));

      const alpha = FILL_ALPHA[theme];
      const outline = OUTLINE[theme];

      for (const index of order) {
        const flat = projected[index];
        const props = propsFor(cells[index], breaks);
        const colour = ramp[props.step];

        let loX = Infinity;
        let hiX = -Infinity;
        let loY = Infinity;
        let hiY = -Infinity;
        const screen = new Float64Array(flat.length);
        for (let i = 0; i < flat.length; i += 2) {
          const [x, y] = project(flat[i], flat[i + 1]);
          screen[i] = x;
          screen[i + 1] = y;
          if (x < loX) loX = x;
          if (x > hiX) hiX = x;
          if (y < loY) loY = y;
          if (y > hiY) hiY = y;
        }

        const size = Math.max(hiX - loX, hiY - loY);
        const mark = MARK_SIZE + props.step * 0.9;

        if (size < MARK_SIZE) {
          // Too small for the outline to mean anything. A mark still reads as activity,
          // and a cell with one detection in it is still a cell worth seeing.
          const cx = (loX + hiX) / 2;
          const cy = (loY + hiY) / 2;
          context.globalAlpha = 0.9;
          context.fillStyle = colour;
          context.beginPath();
          context.arc(cx, cy, mark / 2, 0, Math.PI * 2);
          context.fill();
          context.globalAlpha = 1;
          drawn.current.push({ props, ring: screen, cx, cy });
          continue;
        }

        context.globalAlpha = alpha;
        context.fillStyle = colour;
        context.beginPath();
        context.moveTo(screen[0], screen[1]);
        for (let i = 2; i < screen.length; i += 2) context.lineTo(screen[i], screen[i + 1]);
        context.closePath();
        context.fill();

        context.globalAlpha = 1;
        context.lineWidth = 0.6;
        context.strokeStyle = outline;
        context.stroke();
        drawn.current.push({ props, ring: screen, cx: (loX + hiX) / 2, cy: (loY + hiY) / 2 });
      }
    },
    [cells, breaks, theme, ramp],
  );

  useEffect(() => {
    const target = canvas.current;
    if (!target) return;
    forgetBoundaries();

    // One observer for the lifetime of the mount, not a one-shot. The grid that gives
    // this card its column can land after the first paint, and a canvas sized against a
    // zero-width box draws nothing at all, so the first size that arrives is the one that
    // matters. Staying subscribed also covers the container being `clamp()`ed against the
    // viewport height, which changes with the window.
    let last = "";
    const draw = () => {
      const box = target.getBoundingClientRect();
      const size = `${Math.round(box.width)}x${Math.round(box.height)}`;
      if (size === "0x0" || size === last) return;
      last = size;
      paint(target);
    };

    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(target);
    return () => observer.disconnect();
  }, [paint]);

  function nearest(x: number, y: number) {
    let best: Drawn | null = null;
    let bestDistance = HIT_RADIUS * HIT_RADIUS;
    for (const item of drawn.current) {
      const dx = item.cx - x;
      const dy = item.cy - y;
      const distance = dx * dx + dy * dy;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = item;
      }
    }
    return best;
  }

  // Refs rather than a `useEffect`: a tooltip that opens one frame after the pointer
  // stops is a tooltip nobody sees, and the highlight is what makes a dense field of
  // cells readable at all.
  const readoutRef = useRef<HTMLDivElement>(null);

  function show(x: number, y: number, props: ReturnType<typeof propsFor>) {
    const node = readoutRef.current;
    if (!node) return;
    const readout = readoutFor(props, unit);
    node.children[0].textContent = readout.value;
    node.children[1].textContent = readout.where;
    node.children[2].textContent = readout.scope;
    node.style.left = `${Math.max(x, 4)}px`;
    node.style.top = `${y}px`;
    node.hidden = false;
  }

  function hide() {
    if (readoutRef.current) readoutRef.current.hidden = true;
  }

  return (
    <div ref={host} className="relative h-full w-full">
      <canvas
        ref={canvas}
        className="block h-full w-full"
        aria-hidden="true"
        onPointerMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          const x = event.clientX - box.left;
          const y = event.clientY - box.top;
          const hit = nearest(x, y);
          if (!hit) {
            hide();
            return;
          }
          show(Math.min(x, box.width - 180), Math.max(y - 48, 4), hit.props);
        }}
        onPointerLeave={hide}
      />
      <div
        ref={readoutRef}
        hidden
        className="pointer-events-none absolute z-10 w-max max-w-[200px] rounded-[var(--radius-card)] border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 shadow-lg"
      >
        <div className="text-[13px] font-semibold" />
        <div className="mt-0.5 text-xs text-[var(--color-muted)]" />
        <code className="mt-0.5 block text-[11px] text-[var(--color-subtle)]" />
      </div>
    </div>
  );
}
