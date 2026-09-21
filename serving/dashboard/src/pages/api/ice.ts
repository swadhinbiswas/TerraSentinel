import type { APIRoute } from "astro";
import { iceTrends } from "@/lib/queries";
import { turso } from "@/lib/turso";

export const prerender = false;

export const GET: APIRoute = async ({ request, locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const sinceDays = Number(new URL(request.url).searchParams.get("since") ?? 400);
  const rows = await iceTrends(turso(env), {
    sinceDays: Number.isFinite(sinceDays) ? sinceDays : 400,
  });

  return Response.json(
    { count: rows.length, rows, note: rows.length === 0 ? "sea-ice mart not populated" : undefined },
    { headers: { "cache-control": "public, max-age=900" } },
  );
};
