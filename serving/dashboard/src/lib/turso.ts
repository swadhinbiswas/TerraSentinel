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
  TURSO_AUTH_TOKEN?: string;
}

export function turso(env: TursoEnv): Client {
  if (cached) return cached;

  const url = env.TURSO_DATABASE_URL;
  const authToken = env.TURSO_AUTH_TOKEN;

  if (!url || !authToken) {
    throw new Error(
      "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set as Pages environment " +
        "variables. Locally, put them in serving/dashboard/.dev.vars (gitignored).",
    );
  }

  cached = createClient({ url, authToken });
  return cached;
}

/** Reset the singleton. Used by tests. */
export function resetTurso(): void {
  cached = undefined;
}
