# ADR 0007 — Enumerating the bronze lake outside dbt

**Status:** accepted · implemented in `tools/bronze_manifest.py` and
`transform/macros/bronze_source.sql`

## Context

The staging models read the bronze lake directly over `hf://`. Bronze is partitioned
two ways: live landings at `<source>/region=…/year=…/month=…/day=…/`, and the one-time
historical pull at `backfill/<source>/region=…/year=…/month=…/`. Both layouts matter —
the backfill is what gives anomaly detection a seasonal baseline.

Three approaches were tried, and the first two failed against real data rather than in
review:

1. **A list of two globs**, `[<root>/<source>/**`, `<root>/backfill/<source>/**]`.
   Correct while both layouts exist. Fatal when one does not: immediately after a
   backfill there is no `<source>/` directory, so the live glob 404s and the model dies
   with every row it needs sitting under `backfill/`. That is precisely the
   post-Phase-0 state, and for a weekly source it lasts up to a week.

2. **One leading globstar**, `<root>/**/<source>/**/*.parquet`. Reads backfill-only
   layouts correctly over `hf://` and was verified against 55,677 real rows — then
   broke locally with `IO Error: Cannot use multiple '**' in one path`. DuckDB's local
   glob implementation and its Hugging Face implementation do not accept the same
   patterns, so the SQL stopped working against a local lake while working remotely.

3. **Enumerate, then hand dbt explicit globs** — what we do now. One authenticated,
   retried listing (`tools/bronze_manifest.py`) determines which prefixes exist; dbt
   receives only globs known to match via `--vars '{"bronze_globs": …}'`. When a prefix
   is absent only the other glob is emitted, so a backfill-only or live-only lake both
   read correctly, and every glob contains a single `**` so both filesystems accept it.

## Consequences

- **One extra step before `dbt build`.** Acceptable: it replaces an implicit,
  repeated, per-model listing with a single explicit one, and it is the same listing
  that has to happen anyway to avoid anonymous rate limits.
- **The manifest is computed, never committed.** A stale committed manifest would be a
  new way for the transform to silently read the wrong files.
- **An absent source is a named failure, not a filesystem error.** The model resolves to
  a relation called `bronze_missing_for__<source>__run_backfill_historical_then_rerun`,
  and the resulting "table does not exist" states the fix.
- Deliberately *not* `raise_compiler_error`: that fires at parse time, so one absent
  source would abort every `--select`, including selections that never touch it. A
  runtime failure is confined to the affected branch.
- **A local fallback remains** (the two-glob form) for when `bronze_globs` is absent, so
  local development works without a pre-step. It is safe there because the synthetic
  generator always writes both layouts. CI runs the manifest path, not the fallback.
- A missing source still fails, and the freshness tests say why. Permanent removal of a
  source is a one-line change to `SOURCES` in `collectors/config.py`.

## Alternatives rejected

- **`glob()` at compile time** to test existence — DuckDB's `glob()` against `hf://`
  raises on a missing prefix rather than returning empty, and Jinja has no try/except.
- **`ignore_missing_files`** — no such DuckDB setting.
- **A placeholder parquet in every prefix** to guarantee a match. It would work, but it
  puts junk in a data lake that downstream consumers can see, to solve a problem that
  enumeration solves cleanly.
- **Reading the repo root and filtering by path in SQL** — one glob, but it makes each
  of three models read the entire lake, and `union_by_name` across all sources would
  produce one very wide, very sparse schema.
- **Passing a full file list as a var** — thousands of paths on a command line, and it
  loses the property that adding a partition needs no change anywhere.
