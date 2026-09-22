/**
 * libSQL client for Turso, server-side only.
 *
 * Two rules this file exists to enforce:
 *
 * 1. **Credentials never reach the browser.** The client is created from Cloudflare
 *    Pages environment variables and is only ever imported by API routes and
 *    server-rendered pages. Nothing here may be imported into a React island.
 * 2. **Turso's client is HTTP-based**, which is what makes it usable from `workerd`:
 *    Pages Functions cannot open a raw TCP connection, so a Postgres-style driver
 *    would need a proxy. That is the reason the serving layer is libSQL (ADR 0002).
 */
import { createClient, type Client } from "@libsql/client/web";

let cached: Client | undefined;

export interface TursoEnv {
  TURSO_DATABASE_URL?: string;
  /** Read-only token, preferred everywhere the dashboard reads (which is everywhere). */
  TURSO_TOKEN_RO?: string;
  /** Full-scope token, kept as a fallback for a deployment not yet migrated. */
  TURSO_AUTH_TOKEN?: string;
}

/** The token actually in use: read-only if one is configured.
 *
 *  Reporting the scope has to read the same value `turso()` connects with, or the SQL
 *  console could advertise `ro` while the queries run with `rw`.
 */
export function authToken(env: TursoEnv): string | undefined {
  return env.TURSO_TOKEN_RO ?? env.TURSO_AUTH_TOKEN;
}

export function turso(env: TursoEnv): Client {
  if (cached) return cached;

  const url = env.TURSO_DATABASE_URL;
  // Prefer the read-only token. Every statement this layer issues is a SELECT, so write
  // scope buys nothing here except blast radius: with a write token, a bug in the SQL
  // console's guard has write consequences instead of none. `TURSO_AUTH_TOKEN` stays as
  // the fallback so an unrotated deployment keeps serving rather than failing closed.
  const token = authToken(env);

  if (!url || !token) {
    throw new Error(
      "TURSO_DATABASE_URL and TURSO_TOKEN_RO (preferred) or TURSO_AUTH_TOKEN must be set " +
        "as Pages environment variables. Locally, put them in serving/dashboard/.dev.vars " +
        "(gitignored).",
    );
  }

  cached = createClient({ url, authToken: token });
  return cached;
}

/** Reset the singleton. Used by tests. */
export function resetTurso(): void {
  cached = undefined;
}
