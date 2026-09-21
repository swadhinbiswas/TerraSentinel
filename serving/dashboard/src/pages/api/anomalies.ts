import type { APIRoute } from "astro";
import { anomalies, regions } from "@/lib/queries";
import { turso } from "@/lib/turso";

// A fast SQL read, nothing more. The statistical score and the model percentile are
// precomputed; Pages Functions have no CPU budget for aggregation or inference.
export const prerender = false;

export const GET: APIRoute = async ({ request, locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const url = new URL(request.url);

  if (url.searchParams.get("view") === "regions") {
    return Response.json({ regions: await regions(turso(env)) });
  }

  const sinceDays = Number(url.searchParams.get("since") ?? 400);
  const region = url.searchParams.get("region") ?? undefined;
  const onlyAnomalies = url.searchParams.get("anomalies") === "true";

  const rows = await anomalies(turso(env), {
    sinceDays: Number.isFinite(sinceDays) ? sinceDays : 400,
    region,
    onlyAnomalies,
  });

  return Response.json(
    { count: rows.length, rows },
    { headers: { "cache-control": "public, max-age=300" } },
  );
};
