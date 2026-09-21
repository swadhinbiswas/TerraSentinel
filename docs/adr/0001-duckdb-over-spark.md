# ADR 0001 — DuckDB over Spark for the transform layer

**Status:** accepted · implemented in Phase 3

## Context

The transform layer has to turn a few million rows of sensor observations (fire detections,
NDVI composites, SST anomalies, sea-ice extents) into regional daily/weekly aggregates.
Budget is fixed at zero, and the compute has to run inside a GitHub Actions job with a time
limit and no persistent cluster.

## Decision

`dbt-core` targeting **DuckDB**, reading silver parquet directly from Hugging Face over HTTPS
via `resolve/main` URLs. No warehouse, no cluster, no local copy of the lake.

## Why

- **The data is small.** Two years of three sources is on the order of 10⁵–10⁶ rows of
  numeric features. Spark's value proposition — shuffling data too large for one machine —
  never applies. A single DuckDB process aggregates this in seconds.
- **Zero infrastructure.** DuckDB is an in-process library. There is no service to provision,
  secure, wake up, or pay for. The same `dbt run` works on a laptop and in CI.
- **dbt gives the parts that matter at this scale**: model DAG, dependency-driven execution,
  schema tests, and documentation — without needing a warehouse adapter ecosystem.
- **DuckDB reads the lake over HTTP.** `read_parquet('https://huggingface.co/...')` removes
  the download-and-stage step entirely, which also means CI has no large artifact to cache.

## Consequences

- The transform job must fit in an Actions runner: fine now, and the answer if it stops being
  fine is monthly partitioning plus incremental models, not a cluster.
- DuckDB has no Turso/libSQL adapter. Gold tables are pushed by an explicit, idempotent sync
  script (`sync/push_gold_to_turso.py`) rather than a dbt `target`. Kept as its own pipeline
  step so a sync failure is visible and re-runnable in isolation.
- Concurrency is per-process. Two transform jobs writing the same file would conflict; the
  workflow uses a `concurrency` group to prevent that.

## Alternatives rejected

- **Spark / Databricks / EMR** — a cluster for a dataset that fits in memory. Pure cost and
  operational surface for no capability.
- **BigQuery / Snowflake free tiers** — real value at larger scale, but they introduce a
  warehouse account, credentials, quotas, and a vendor SDK dependency into a project whose
  whole premise is that a clone of the repo runs end to end.
- **Polars alone** — comparable performance, but no model DAG, no test framework, no
  documentation generation. dbt's structure is the point, not the SQL engine.
