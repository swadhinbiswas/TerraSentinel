# ADR 0009 — Mirror the bronze lake locally before transforming

**Status:** accepted · implemented in `tools/sync_bronze.py`

## Context

Staging models read bronze parquet directly over `hf://`, which reads beautifully
and behaves badly in production. DuckDB's Hugging Face filesystem **lists the
repository tree on every query that references a remote path**, and the Hub allows
1000 API requests per 5 minutes.

Measured consequences:

* With staging as **views**, each of ~50 dbt tests re-queried the remote path, and
  each query re-listed the tree. One build attempted 50+ listings and was
  rate-limited before it finished.
* As **tables** it is still several listings per build, plus an HTTPS round trip per
  file per read.
* Any HTTP hiccup fails a model four layers deep, with an error that names a repo
  path rather than the real problem.

That is the wrong foundation for a transform job, however elegant the SQL looks.

## Decision

**Mirror the lake to a local directory, then transform entirely locally.**

`tools/sync_bronze.py` performs one listing and then fetches files in parallel over
the CDN (not the API), so the API cost of a transform run becomes **one request
instead of dozens**. The SQL is unchanged; only `bronze_root` points at the mirror.

Measured on the real lake: **102 files, 8.8 MB, 18 seconds**, after which a full
117-node build with all schema tests runs in **4 seconds** with zero API calls.

`--revision` pins a specific lake state, which is also what a model's
dataset-commit lineage should point at.

## Consequences

- **The rate-limit failure mode disappears.** CI never had it (local synthetic
  data); production would have hit it on every run.
- The build gets faster, because local I/O replaces per-file HTTPS range requests.
- Disk cost is trivial and bounded by design: bronze holds numeric features, not
  imagery. A runner with 14 GB has no difficulty.
- `tools/bronze_manifest.py` is still needed: a glob for a missing directory is an
  error even locally, and right after a backfill there is no live prefix. It is now
  a filesystem walk, so it costs nothing.
- `tools/configure_duckdb_secret.py` is retained but no longer part of the transform
  workflow — it is only needed if someone points `bronze_root` back at `hf://`.
- The mirror is a cache, not a source of truth. Nothing writes to it, and deleting
  it costs one re-download.

## Alternatives rejected

- **Keep reading `hf://` with staging materialised** — fewer listings, still
  API-dependent, still remote I/O per file, and still one failure mode away from a
  broken model. This was tried and it 429'd.
- **Pass an explicit file list as a dbt var** — removes listing but not the per-file
  remote reads, and thousands of paths do not belong on a command line.
- **Download only the partitions a run needs** — tempting for a daily job, but it
  makes the transform's inputs vary with wall-clock time, which is exactly how a
  pipeline stops being reproducible.
