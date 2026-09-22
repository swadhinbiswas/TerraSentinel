/**
 * Guard for the SQL console.
 *
 * The strongest control is a read-only Turso token, and the dashboard now prefers one:
 * `turso()` connects with `TURSO_TOKEN_RO` whenever it is configured, so a bug in this
 * guard has no write consequences at all. Until that token is issued, the connection may
 * still carry write scope (the JWT's `a` claim is `rw`), which makes an unguarded console
 * a way for anyone with the page to destroy the gold tables — so the guard stays, as
 * defence in depth rather than as the only line. It is deliberately paranoid and refuses
 * rather than sanitises:
 *
 *   * one statement only — a trailing semicolon is allowed, anything after is not
 *   * must begin with SELECT or WITH
 *   * a deny-list of mutating and schema keywords, checked as whole words so a column
 *     named `created_at` does not trip it
 *   * a row cap appended when the query does not already limit itself
 *
 * `tokenScope()` reports what the token actually in use allows, so the console says so
 * out loud instead of implying a safety it may not have.
 */

const DENIED = [
  "insert", "update", "delete", "drop", "alter", "create", "replace", "attach", "detach",
  "pragma", "vacuum", "reindex", "trigger", "begin", "commit", "rollback", "savepoint",
  "grant", "revoke", "analyze",
];

export const MAX_ROWS = 500;

export interface GuardResult {
  ok: boolean;
  sql?: string;
  reason?: string;
}

export function guardSql(raw: string): GuardResult {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return { ok: false, reason: "empty query" };

  // Strip a single trailing semicolon, then refuse any other semicolon: that is what
  // separates "one statement" from "a script".
  const withoutTrailing = trimmed.replace(/;\s*$/, "");
  if (withoutTrailing.includes(";")) {
    return { ok: false, reason: "only one statement per query is allowed" };
  }

  // Comments could hide a second statement or a denied keyword from a naive check.
  if (withoutTrailing.includes("--") || withoutTrailing.includes("/*")) {
    return { ok: false, reason: "comments are not allowed in console queries" };
  }

  if (!/^\s*(select|with)\b/i.test(withoutTrailing)) {
    return { ok: false, reason: "only SELECT and WITH queries are allowed" };
  }

  for (const keyword of DENIED) {
    if (new RegExp(`\\b${keyword}\\b`, "i").test(withoutTrailing)) {
      return { ok: false, reason: `the keyword '${keyword}' is not allowed` };
    }
  }

  const alreadyLimited = /\blimit\s+\d+/i.test(withoutTrailing);
  const sql = alreadyLimited
    ? withoutTrailing
    : `${withoutTrailing} limit ${MAX_ROWS}`;

  return { ok: true, sql };
}

/** The `a` claim of a Turso JWT: `ro` for read-only, `rw` for read-write. */
export function tokenScope(authToken: string | undefined): "ro" | "rw" | "unknown" {
  if (!authToken) return "unknown";
  const parts = authToken.split(".");
  if (parts.length < 2) return "unknown";
  try {
    const payload = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = payload.padEnd(payload.length + ((4 - (payload.length % 4)) % 4), "=");
    const decoded = JSON.parse(atob(padded)) as { a?: string };
    return decoded.a === "ro" || decoded.a === "rw" ? decoded.a : "unknown";
  } catch {
    return "unknown";
  }
}
