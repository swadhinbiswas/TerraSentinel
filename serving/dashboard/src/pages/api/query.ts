import type { APIRoute } from "astro";
import { runGuardedQuery } from "@/lib/queries";
import { tableStatus } from "@/lib/registry";
import { MAX_ROWS, tokenScope } from "@/lib/sql-guard";
import { authToken, turso } from "@/lib/turso";

export const prerender = false;

/** Table names and columns, so the console can offer the schema it is querying. */
export const GET: APIRoute = async ({ locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const statuses = await tableStatus(turso(env));

  return Response.json({
    maxRows: MAX_ROWS,
    // Stated openly: the guard is the control on this route, and if the token is
    // read-write then a bug in the guard has write consequences. The dashboard prefers a
    // read-only token (`TURSO_TOKEN_RO`) precisely so that cannot happen; this reports
    // the scope of whichever token the connection actually uses.
    tokenScope: tokenScope(authToken(env)),
    tables: statuses.map((status: { name: string; title: string; present: boolean; rows: number | null; columns: string[] }) => ({
      name: status.name,
      title: status.title,
      present: status.present,
      rows: status.rows,
      columns: status.columns,
    })),
  });
};

export const POST: APIRoute = async ({ request, locals }) => {
  const env = locals.runtime?.env ?? import.meta.env;
  const body = (await request.json().catch(() => ({}))) as { sql?: string };

  try {
    const result = await runGuardedQuery(turso(env), body.sql ?? "");
    return Response.json(result);
  } catch (error) {
    // A rejected query is a normal outcome, not a server fault, but the reason has to
    // come back intact so the console can show it next to the query.
    return Response.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 400 },
    );
  }
};
