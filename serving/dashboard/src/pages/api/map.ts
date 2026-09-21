import type { APIRoute } from "astro";
import { mapWindow } from "@/lib/queries";
import { turso } from "@/lib/turso";

export const prerender = false;

/**
 * Map cells for a date window, optionally anomalies only.
 *
 * Hex geometry is computed in the browser by h3-js from the cell id, so the database
 * stores no polygons and a cell's shape can never be stale.
 */
export const GET: APIRoute = async ({ request, locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const url = new URL(request.url);

  const payload = await mapWindow(turso(env), {
    layer: url.searchParams.get("layer") === "sst" ? "sst" : "fire",
    from: url.searchParams.get("from") ?? undefined,
    to: url.searchParams.get("to") ?? undefined,
    mode: url.searchParams.get("mode") === "anomalies" ? "anomalies" : "all",
    region: url.searchParams.get("region") ?? undefined,
    limit: Number(url.searchParams.get("limit") ?? 6000),
  });

  return new Response(JSON.stringify(payload), {
    headers: {
      "content-type": "application/json",
      // The dataset changes on a schedule, not per request.
      "cache-control": "public, max-age=300, stale-while-revalidate=3600",
    },
  });
};
