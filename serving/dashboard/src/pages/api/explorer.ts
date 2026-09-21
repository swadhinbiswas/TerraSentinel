import type { APIRoute } from "astro";
import { explorerPage } from "@/lib/queries";
import { turso } from "@/lib/turso";

export const prerender = false;

export const GET: APIRoute = async ({ request, locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const url = new URL(request.url);

  try {
    const page = await explorerPage(turso(env), {
      table: url.searchParams.get("table") ?? "",
      limit: Number(url.searchParams.get("limit") ?? 50),
      offset: Number(url.searchParams.get("offset") ?? 0),
      region: url.searchParams.get("region") ?? undefined,
      sinceDays: url.searchParams.get("since") ? Number(url.searchParams.get("since")) : undefined,
    });
    return Response.json(page);
  } catch (error) {
    // An unregistered table name is a client error; saying which table was refused is
    // more useful than a generic 500.
    return Response.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 400 },
    );
  }
};
