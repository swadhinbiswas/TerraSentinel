# ADR 0002 — Turso (libSQL) over Postgres for the serving layer

**Status:** accepted

## Context

The dashboard needs to read precomputed anomaly tables with low latency from Cloudflare's
edge runtime. Writes happen only from scheduled batch jobs (gold sync, batch scoring).
Postgres is the default answer for "a database", so the choice needs justifying.

## Decision

**Turso** (libSQL), queried from Cloudflare Pages Functions with the HTTP pipeline API.
Writers use idempotent upserts keyed on a primary key; the dashboard only ever reads.

## Why

- **The runtime dictates the driver.** Pages Functions run in `workerd`, which cannot open a
  raw TCP connection to Postgres. Reaching Postgres would require Hyperdrive or a proxy
  service — an extra moving part on the request path, and another thing to keep inside a
  free tier. libSQL's HTTP protocol works from `workerd`, from CPython, and from CI with the
  same request shape.
- **Read latency where the users are.** Turso replicates to edge locations, which is the
  whole reason to pair it with Cloudflare Pages.
- **Writes are batch and small.** The gold layer is aggregated, not raw, so the free tier's
  storage ceiling is not a constraint. Write volume is a few thousand rows per day.
- **One transport, one typing rule set.** `storage/turso_client.py` implements the JSON
  pipeline protocol directly. The sync job and the dashboard share that understanding of
  types, errors and retries, rather than each binding to a different client library — and it
  sidesteps the fact that the official Python clients are still churning.

## Consequences

- **No dbt adapter.** Turso is not a dbt target; the sync is a script. Accepted deliberately:
  a thin, explicit, idempotent step is more debuggable than an immature adapter.
- **Native SQL types are limited** (SQLite affinity). Timestamps are stored as ISO-8601 UTC
  text, which sorts correctly and is unambiguous. `pipeline_runs.details` is JSON text.
- **`INSERT … ON CONFLICT` is the only write path** exposed by the client. A re-run or a
  partially failed sync converges on the same table contents instead of double-counting,
  which matters because Actions jobs are routinely re-run.

## Alternatives rejected

- **Postgres on Neon/Supabase** — excellent databases, wrong runtime. Would need Hyperdrive or
  a separate API service purely to bridge the protocol gap.
- **Cloudflare D1** — the natural sibling to Pages, but no access path from a GitHub Action
  without a Worker in front, and the transform/scoring jobs are the primary writers.
- **Reading parquet directly from the dashboard** — would push aggregation and join logic into
  the edge function, exactly where there is no CPU budget for it.
