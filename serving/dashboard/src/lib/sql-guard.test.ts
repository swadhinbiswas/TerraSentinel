import { describe, expect, it } from "vitest";
import { guardSql, MAX_ROWS, tokenScope } from "./sql-guard";

/**
 * The SQL guard.
 *
 * This is the one place on the dashboard where a wrong answer is a security problem rather
 * than a wrong number, so the cases below are the ones a real exploit would try rather than
 * the ones that happen to come up in normal use.
 */
describe("guardSql", () => {
  it("allows a plain SELECT and appends the row cap", () => {
    const result = guardSql("select * from gold_fire_anomalies");
    expect(result.ok).toBe(true);
    expect(result.sql).toBe(`select * from gold_fire_anomalies limit ${MAX_ROWS}`);
  });

  it("leaves an existing limit alone rather than stacking a second one", () => {
    const result = guardSql("select region_id from gold_fire_anomalies limit 5");
    expect(result.sql).toBe("select region_id from gold_fire_anomalies limit 5");
  });

  it("allows a single trailing semicolon", () => {
    expect(guardSql("select 1;").ok).toBe(true);
  });

  it("refuses a script that hides a second statement after a semicolon", () => {
    const result = guardSql("select 1; drop table gold_fire_anomalies");
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/one statement/i);
  });

  it("refuses comments, which can hide a keyword from a naive check", () => {
    expect(guardSql("select 1 -- and more").ok).toBe(false);
    expect(guardSql("select 1 /* hidden */").ok).toBe(false);
  });

  it("allows WITH", () => {
    expect(guardSql("with recent as (select 1) select * from recent").ok).toBe(true);
  });

  it.each([
    "insert into gold_fire_anomalies values (1)",
    "update gold_fire_anomalies set zscore = 0",
    "delete from gold_fire_anomalies",
    "drop table gold_fire_anomalies",
    "alter table gold_fire_anomalies add column x integer",
    "create table t (a int)",
    "attach database 'other.db' as other",
    "pragma table_info(gold_fire_anomalies)",
    "vacuum",
    "reindex",
    "begin",
    "commit",
    "rollback",
    "grant all on gold_fire_anomalies to nobody",
  ])("refuses %s", (sql) => {
    const result = guardSql(sql);
    expect(result.ok).toBe(false);
    // A refusal that does not say why is as useless as no guard at all.
    expect(result.reason).toBeTruthy();
  });

  it("refuses a non-SELECT leading keyword", () => {
    expect(guardSql("explain select 1").ok).toBe(false);
    expect(guardSql("  \n select 1").ok).toBe(true);
  });

  it("does not trip on a denied word appearing inside an identifier", () => {
    // `created_at` contains `create`. Matching on whole words is the whole point.
    expect(guardSql("select created_at from pipeline_runs").ok).toBe(true);
    expect(guardSql("select commit_id from pipeline_runs").ok).toBe(true);
    expect(guardSql("select updated_at, rolled_back from pipeline_runs").ok).toBe(true);
  });

  it("catches a denied keyword regardless of case", () => {
    expect(guardSql("SELECT 1 UNION SELECT * FROM t WHERE 1=1; DROP TABLE x").ok).toBe(false);
    expect(guardSql("select * from t where a = 'delete'").ok).toBe(false);
  });

  it("rejects an empty query", () => {
    const result = guardSql("   ");
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/empty/i);
  });

  it("never returns a sql string when it refuses", () => {
    for (const bad of ["drop table t", "select 1; select 2", "select 1 -- x", ""]) {
      const result = guardSql(bad);
      if (!result.ok) expect(result.sql).toBeUndefined();
    }
  });
});

/** `tokenScope` decodes the `a` claim out of a Turso JWT. */
describe("tokenScope", () => {
  const jwt = (payload: Record<string, unknown>) =>
    `${btoa('{"alg":"HS256"}')}.${btoa(JSON.stringify(payload))}.sig`;

  it("reads a read-write scope", () => {
    expect(tokenScope(jwt({ a: "rw" }))).toBe("rw");
  });

  it("reads a read-only scope", () => {
    expect(tokenScope(jwt({ a: "ro" }))).toBe("ro");
  });

  it("treats an unrecognised scope as unknown rather than read-only", () => {
    // Defaulting an unknown claim to `ro` would report a weaker scope than the token
    // actually carries, which is the one direction that must never happen silently.
    expect(tokenScope(jwt({ a: "admin" }))).toBe("unknown");
    expect(tokenScope(jwt({}))).toBe("unknown");
  });

  it("survives a missing or malformed token", () => {
    expect(tokenScope(undefined)).toBe("unknown");
    expect(tokenScope("")).toBe("unknown");
    expect(tokenScope("not-a-jwt")).toBe("unknown");
    expect(tokenScope("a.b")).toBe("unknown");
  });
});
