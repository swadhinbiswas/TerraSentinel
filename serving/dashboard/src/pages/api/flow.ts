import type { APIRoute } from "astro";
import { flowReport } from "@/lib/flow";
import { turso } from "@/lib/turso";

export const prerender = false;

/**
 * The pipeline, for the live view.
 *
 * `no-store` because the whole point of the endpoint is to be polled: the freshness
 * figures are computed against `Date.now()` on the server, so a cached copy would report
 * a pipeline that is current at the moment it was cached and quietly wrong afterwards.
 */
export const GET: APIRoute = async ({ locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const report = await flowReport(turso(env));
  return Response.json(report, { headers: { "cache-control": "no-store" } });
};
