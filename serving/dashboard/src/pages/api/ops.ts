import type { APIRoute } from "astro";
import { opsReport } from "@/lib/queries";
import { turso } from "@/lib/turso";

export const prerender = false;

export const GET: APIRoute = async ({ locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  return Response.json(await opsReport(turso(env)), {
    headers: { "cache-control": "no-store" },
  });
};
