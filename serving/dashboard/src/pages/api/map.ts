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

  const body = JSON.stringify(payload);

  // Strong validator over the exact bytes, so a 304 can never hand back a body the client
  // already has but that has since changed. This matters more than it looks: a full season
  // of daily cells is a megabyte of JSON, and without a validator every revalidation after
  // the 300s freshness window re-downloads all of it even though the underlying data
  // changes a few times a day. The browser revalidates transparently — no client change
  // needed — so the saving lands on the second and later visits without anyone thinking
  // about it.
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  const etag = `"${[...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("")}"`;

  if (request.headers.get("if-none-match") === etag) {
    return new Response(null, {
      status: 304,
      headers: {
        etag,
        // A 304 must carry the same freshness as the 200 it replaces, or the client
        // revalidates again immediately and the saving is thrown away.
        "cache-control": CACHE,
      },
    });
  }

  return new Response(body, {
    headers: {
      "content-type": "application/json",
      etag,
      "cache-control": CACHE,
    },
  });
};

/**
 * Five minutes fresh, then revalidated for an hour.
 *
 * The pipeline publishes on a cron measured in hours, so a five-minute window is already
 * more responsive than the data. `stale-while-revalidate` means a reader mid-interaction
 * never waits on the origin: the browser serves the cached window immediately and updates
 * it in the background, which is why this can be generous.
 */
const CACHE = "public, max-age=300, stale-while-revalidate=3600";
