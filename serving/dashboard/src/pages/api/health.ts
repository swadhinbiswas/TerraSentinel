import type { APIRoute } from "astro";
import { health } from "@/lib/queries";
import { turso } from "@/lib/turso";

export const prerender = false;

// Answers "is the pipeline healthy" from data — the last successful run per workflow and
// which marts exist — rather than from an uptime ping. This is what the dashboard's
// "last updated" indicator reads, and it reports absent marts instead of hiding them.
export const GET: APIRoute = async ({ locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const report = await health(turso(env));

  return Response.json(report, {
    status: report.ok ? 200 : 503,
    headers: { "cache-control": "no-store" },
  });
};
