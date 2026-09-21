# TerraSentinel

Anomaly detection on free public satellite and sensor data — wildfire, deforestation and
glacier/ice melt — running end to end on free tiers. Ingestion, a versioned data lake,
SQL transforms, unsupervised ML, and an edge-served dashboard.

**Cost: $0.** GitHub Actions does the compute, Hugging Face Hub stores the lake and the
model registry, Turso serves the gold tables, Cloudflare Pages serves the dashboard.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Historical backfill (FIRMS, Sentinel, NOAA/NSIDC) → HF bronze | NOAA/NSIDC **done for real**; FIRMS/GEE awaiting keys |
| 1 | FIRMS → bronze → dbt staging → Turso → one API route | collectors + staging + sync done; API route pending |
| 2 | Sentinel (GEE) + NOAA/NSIDC collectors, unified silver contract | collectors done |
| 3 | dbt staging/intermediate/gold + automated Turso sync | **done** — 117 nodes, all tests green |
| 4 | Feature table, IsolationForest, MLflow, HF model registry, batch scoring | **done** |
| 5 | Known-events validation + synthetic anomaly injection in CI | **done** |
| 6 | Astro + shadcn/ui dashboard on Cloudflare Pages | **deployed** — https://terrasentinel-dashboard.pages.dev |
| 7 | Evidently drift check → auto-retrain, alerting on every workflow | alerting done; drift pending |
| 8 | ADRs | written |

`497` unit tests, a full dbt build, and a type-checked dashboard build, all offline. The transform layer runs against a
synthetic lake with deliberately injected anomalies, and CI asserts the gold marts actually
flag them — see "Proving it works" below.

**Live:** [terrasentinel-dashboard.pages.dev](https://terrasentinel-dashboard.pages.dev) — 8 pages
on Cloudflare's edge, reading Turso directly from workerd with no backend service in between.

**Serving database live:** 76,387 rows synced into Turso
(`terra-globalopenresearch`, ap-south-1) — four gold marts plus 1,460 model predictions.
The dashboard reads it at sub-10 ms through Pages Functions.

**Model published:** [`swadhinbiswas/TerraSentinel-models`](https://huggingface.co/swadhinbiswas/TerraSentinel-models)
— an IsolationForest on 27 strictly causal features, traced to bronze commit `445d10f3`.
Its card records a measured limitation rather than hiding it: at the serving threshold its
flagged slice is *identical* to the statistical rule's (37/37 days, Jaccard 1.0), because
the top 2.5% of fire days are ~20× the rest and therefore trivially separable.

**Live as of this run:** 55,677 real NOAA/NSIDC rows landed in
[`swadhinbiswas/TerraSentinel`](https://huggingface.co/datasets/swadhinbiswas/TerraSentinel)
in a single commit, and the transform read them back off the Hub. Arctic sea-ice extent in
June 2026 is running **~1.3 × 10⁶ km² below the 1981–2010 normal (z ≈ −3.3)** — a real
result from real data.

## Storage layout

Two repos, both under the same namespace:

| Repo | Type | Holds |
|---|---|---|
| `<namespace>/TerraSentinel` | dataset | the bronze lake: `firms/`, `sentinel/`, `noaa_nsidc/`, `backfill/`, `ops/` |
| `<namespace>/TerraSentinel-models` | model | model registry (Phase 4) |

**Why dataset repos and not buckets.** The spec requires MLflow to log the exact dataset
*commit hash* behind every training run, so a model can always be traced to its data. Dataset
repos are git-backed, which is what gives that a real answer; the `hf buckets` commands expose
no revisions or commit messages. Buckets are also surfaced through a separate CLI
(`hf buckets create` vs `hf repos create --type dataset`) and a different DuckDB path form.

A model registry cannot share the dataset repo, because repo *type* is part of the address on
the Hub and model cards and revisions are model-repo features.

`HF_NAMESPACE` is the only naming input; the project name is a constant in
`collectors/config.py`. Any single store can be pointed elsewhere with `HF_BRONZE_REPO`,
`HF_SILVER_REPO` or `HF_MODEL_REPO`.

**Reading the lake needs authentication.** DuckDB's `hf://` filesystem goes out anonymously
unless a `HUGGINGFACE` secret exists, and anonymous reads share the Hub's public rate-limit
pool — a scheduled job will hit HTTP 429 on the repo tree API.
`tools/configure_duckdb_secret` registers it, deliberately *outside* dbt so the token never
reaches compiled SQL or an uploaded artifact.

## Architecture

```
NASA FIRMS ─┐
Sentinel-2/1 (GEE) ─┼─► collectors ─► HF bronze (parquet, versioned)
NOAA / NSIDC ─┘                        │
                                       ├─► dbt + DuckDB ─► gold marts ─► Turso (libSQL)
                                       │                                   │
                                       └─► features ─► unsupervised ML ────┤
                                                          │                │
                                              HF model registry    Astro API routes
                                                                   (Cloudflare Pages)
```

There is deliberately **no live Python inference on the request path**. Batch scoring applies
the registered model to the latest gold features and writes `anomaly_score` into Turso as part
of the scheduled job; the dashboard's API routes only ever do a fast SQL read. That is what
keeps the edge functions inside Cloudflare's CPU budget and the dashboard fast.

### The transform layer

```
bronze (parquet on HF, read directly over https)
  └─ staging      stg_firms        deduped detections, MODIS/VIIRS brightness unified
                  stg_sentinel     region series + res-5 grid change
                  stg_noaa         SST anomaly, sea-ice extent, 1981-2010 climatology
       └─ intermediate
                  int_fire_daily_region      dense region x day spine (zero days exist)
                  int_fire_daily_h3          sparse per-cell counts for the map
                  int_seasonal_fire_baseline median + MAD over a circular +/-15 day window
                  int_sentinel_monthly_region monthly values with the prior year alongside
            └─ gold  (one mart per source arm — see below)
                  gold_fire_anomalies         daily fire z-score, severity, ML score slot
                  gold_ice_extent_trends      sea ice vs the NSIDC 1981-2010 normal
                  gold_deforestation_index    year-over-year NDVI change, lower tail
                  gold_glacier_backscatter    SAR year-over-year change
                  gold_h3_fire                fire cells, res 7 — the map
                  gold_h3_sst                 marine heatwave cells, res 5
                  gold_h3_sentinel            NDVI/SAR change cells, res 5
```

**Why one mart per source arm.** A dbt union requires every input to exist, so a
combined "all ice" mart meant a missing Sentinel source also removed the sea-ice results —
data that was present, healthy, and unrelated to Sentinel. The same applied to a single
combined map mart. Splitting them means an outage can only remove its own table, and the
dashboard reads whichever exist.

The transform **mirrors the lake locally first** (`tools/sync_bronze.py`: one listing
plus parallel CDN fetches — 102 files / 8.8 MB / 18 s measured), then builds entirely
off local disk. DuckDB's `hf://` filesystem re-lists the repo tree on every query that
touches a remote path, which is fine for a notebook and fatal for a scheduled build:
with staging as views, ~50 dbt tests meant 50+ tree listings against a
1,000-per-5-minutes API quota. After mirroring, a full 117-node build makes **zero** API
calls and runs in ~4 seconds.

*Which* globs dbt reads is still decided outside dbt by `tools/bronze_manifest.py`. That indirection exists
because both simpler options failed against real data: a two-glob list
(`<source>/**` plus `backfill/<source>/**`) dies the moment a prefix is missing, which is
exactly the state after a backfill, and a single `**` pattern works over `hf://` but not on a
local filesystem. So the listing happens once, authenticated and retried, and dbt receives
only globs known to match. A source with no files at all fails its own branch with a relation
named `bronze_missing_for__<source>__run_backfill_historical_then_rerun`.

Partitioning is switched **off** when reading: live and backfill landings have different hive
key sets (only live paths carry `day=`) and DuckDB refuses to read both in one call. Nothing is
lost, because `region_id` is a real column on every row — which also decouples the models from
the physical layout entirely.

## Anomaly definitions

Stated explicitly rather than buried, because "anomaly" is the whole product:

| Mart | Signal | Compared against |
|---|---|---|
| `gold_fire_anomalies` | daily detection count | median of the same ±15 days across years, scaled MAD as the yardstick |
| `gold_deforestation_index` | monthly NDVI | the same month a year earlier; z-score against that region's own change distribution |
| `gold_ice_melt_trends` (extent) | daily sea-ice extent | the published NSIDC 1981–2010 per-day normal and its standard deviation |
| `gold_ice_melt_trends` (SAR) | monthly backscatter | year-over-year change distribution (no climatology exists; `baseline_source` says so) |

Two choices are worth calling out. **Median/MAD, not mean/stddev**, for fire: a large fire
sits inside its own baseline window, and with mean/stddev it inflates the very yardstick it is
measured against — partly hiding itself. **A ±15-day pooled window, not "same date last
year"**: with two years of history, a same-date baseline has a sample size of two.

### The ML layer

```
gold_fire_anomalies ─► build_feature_table ─► train_isolation_forest ─► ModelBundle
                        (27 causal features)         │                      │
                                                     ▼                      ▼
                                          MLflow (dataset commit)   HF model repo
                                                                            │
                                    ml_predictions ◄── batch_score ◄────────┘
                                    (serving DB)
```

Three properties are load-bearing:

- **Every feature is causal.** Rolling windows end at `1 preceding`, so no feature can see
  the day it scores. The statistical `zscore` is deliberately *not* a feature — it is
  computed from the whole history including the future, and using it would make the
  comparison between model and rule circular.
- **The score is a percentile**, ranked against the training distribution stored in the
  bundle, so "0.98" means the same thing at serving time as it did at training time.
- **Evaluation reports what is measurable without labels**: distribution shape, separation
  from the rest of the data, agreement with the rule (as a weak comparator), and the
  documented real events. Never accuracy or F1.

## The dashboard

```
serving/dashboard/   Astro + React islands + Tailwind, deployed to Cloudflare Pages
  src/lib/turso.ts       libSQL client — server-side ONLY, never imported by an island
  src/lib/queries.ts     every read the dashboard performs, one place
  src/lib/registry.ts    the data catalog, and the explorer's table allowlist
  src/lib/sql-guard.ts   SELECT-only guard for the console
  src/lib/stories.ts     investigations that query the mart at render time
  src/pages/api/         health · anomalies · ice · map · query · explorer · ops
  src/components/        AnomalyMap, TrendChart, IceChart, SeasonalProfile,
                         ExplorerTable, SqlConsole
```

Eight pages: **Overview** · **Analysis** · **Stories** · **Catalog** · **Explorer** · **SQL** ·
**Ops** · **Docs**.

### UI decisions that were forced by bugs

Three things about this UI are the way they are because the obvious version did not work:

**The charts are hand-rolled SVG, not a charting library.** Recharts failed three separate
ways: v2 predates React 19, v3 mis-measured its container (rendering squashed into a
corner), and an island that fails to hydrate renders *nothing*. For one area series, one
bar series and two lines, computed SVG paths render **on the server** — so the first paint
contains the chart, a JS failure cannot blank the panel, and 395 KB of gzipped dependency
disappeared. Tooltips are `<title>` elements: native, keyboard-accessible, no JavaScript.

**The map uses dual encoding (dots *and* hexagons).** Measured: an H3 res-7 cell is ~1.1 km
across, which is **0.23 px at zoom 5** and still under 4 px at zoom 9. Drawing only
polygons means the map looks empty at every zoom a region-wide view needs — which is
exactly how it shipped once. Dots have a pixel-radius floor and are always visible;
hexagon boundaries appear from zoom 7 where the shape is legible.

**The camera never animates.** `fitBounds` with a duration is a *motion*, and a motion that
does not run leaves the camera on its default centre. This map rendered zero features while
holding 4,392 of them, for exactly that reason. Framing is now instantaneous and driven by a
`ResizeObserver` on the container, because the container has no size until the stylesheet
gives the grid its columns.

**The colour ramp varies in luminance, not hue.** A red→green severity scale is invisible to
the ~8% of men with red-green deficiency, and red-green is the obvious choice for severity.
`--color-i1`..`--color-i5` run from a deep ember to near-white, and every value is also
labelled in text — colour is a redundant channel, never the only one.

Two accessibility details that cost nothing: focus rings are never suppressed, and
`prefers-reduced-motion` is honoured rather than overridden.

Four API routes, all single indexed reads against precomputed tables. Nothing on the
request path aggregates, loads a model, or infers — that work happens in scheduled
GitHub Actions jobs, because Pages Functions have a short CPU budget on the free tier.

Three deliberate choices:

- **Server-rendered first paint.** `index.astro` queries Turso during the request, so
  the HTML arrives with real numbers. Only the map and the chart are `client:only`
  islands; everything else is static.
- **Degradation over 500s.** Three marts depend on Sentinel data that is not backfilled,
  so `tableExists` is checked and a missing table empties its own panel. `/api/health`
  reports exactly which marts exist and their row counts.
- **Hexagons are real H3 cell boundaries**, not dots. A res-7 cell is ~5 km² and a
  res-5 cell is ~253 km²; drawing both as a uniform point would imply precision the
  coarse layer does not have. The legend states the resolution of the layer on screen.

Turso credentials are Cloudflare Pages environment variables, read server-side. The
browser only ever talks to the site's own `/api/*`.

### Deploying

```bash
cd serving/dashboard
npm run build
npx wrangler pages deploy dist --project-name=terrasentinel-dashboard --branch=main
```

Two things that must be right, both of which cost time to discover:

- **`compatibility_flags = ["nodejs_compat"]`** in `wrangler.toml`. Astro's image service pulls
  in `sharp`, which requires node builtins even though this dashboard renders no images.
  Without the flag, the worker fails to bundle.
- **React 18, not 19.** React 19's server renderer uses `MessageChannel`, which workerd does
  not provide, so the worker dies at startup with `ReferenceError: MessageChannel is not
  defined`. React 18 avoids that code path. Verified by running the built output under
  `wrangler pages dev`, which is the only way to catch it: the Node-based `astro dev` server
  is a different runtime and never hits it.

## Proving it works

An unsupervised model can always claim to work. So the pipeline ships a falsifiable check:

```bash
python -m tools.synthetic_bronze --out data/bronze --clean --inject-anomaly
dbt build --project-dir transform --profiles-dir transform --vars '{"bronze_root": "data/bronze"}'
python -m tools.assert_anomalies_detected --duckdb-path transform/terrasentinel.duckdb
```

The generator plants one grossly obvious event per anomaly type (a 60-detection fire day, a
0.35 NDVI collapse, a 1.8 × 10⁶ km² sea-ice excursion, a 3 °C marine heatwave). The checker
asserts each is flagged **and** that the overall flag rate stays under 2% — because a
degenerate baseline that flags everything would sail past a naive sensitivity check. CI runs
exactly this. The last run: fire z=16.9 extreme, NDVI z=-3.1 high, ice z=-4.3 extreme, fire
flag rate 0.62%.


## Layout

```
collectors/      source collectors + shared retry/breaker/validation machinery
storage/         HF Hub writer/reader, libSQL HTTP client
transform/       dbt project (DuckDB): staging → intermediate → gold
ml/              features, training, registry, scoring, validation, drift
sync/            gold marts → Turso (idempotent upsert)
ops/             alerting, run metadata, redaction, resilience
serving/         Astro dashboard (Phase 6)
pandera_schemas/ the data contracts between layers
tools/           synthetic lake generator, seed export, anomaly assertions
tests/           357 tests plus a full dbt build, all offline
docs/adr/        why the non-obvious choices were made
```

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"          # Python 3.12 pinned: GEE and torch lag newer releases
cp .env.example .env                # fill in credentials (never committed)
```

Optional extras: `[gee]` Sentinel via Earth Engine, `[noaa]` OPeNDAP, `[transform]` dbt+DuckDB,
`[ml]` scikit-learn, `[drift]` Evidently.

### Credentials

| Variable | Needed for | Where to get it |
|---|---|---|
| `HF_TOKEN`, `HF_NAMESPACE`, `HF_PROJECT` | bronze/silver datasets, model registry | huggingface.co/settings/tokens |
| `FIRMS_MAP_KEY` | fire detections | firms.modaps.eosdis.nasa.gov/api/map_key |
| `GEE_SERVICE_ACCOUNT_JSON` / `_EMAIL` / `GEE_PROJECT` | Sentinel-2/-1 | free for research; register a service account for Earth Engine |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | gold/serving DB | turso.tech free tier |
| `ALERT_WEBHOOK_URL` | failure alerts | Slack or Discord incoming webhook |

NOAA and NSIDC need no credentials. Nothing reads a secret from anywhere but the
environment, and everything that formats text for a human passes through `ops/redact.py`
first — including URLs, because FIRMS puts its key in the request *path*.

## Running

```bash
# what would a backfill cost, in requests?
python -m collectors.backfill_historical --sources all --plan

# one-time history before go-live (run this before scheduling anything)
python -m collectors.backfill_historical --start-date 2024-09-01

# a single source, no credentials required
python -m collectors.noaa_nsidc_collector --regions arctic --dry-run

# transform layer: build models + tests against local synthetic bronze
python -m tools.synthetic_bronze --out data/bronze --clean
dbt build --project-dir transform --profiles-dir transform \
          --vars '{"bronze_root": "data/bronze"}'

# publish gold marts to Turso (idempotent upsert; --dry-run shapes and checks without writing)
python -m sync.push_gold_to_turso --duckdb-path transform/terrasentinel.duckdb --dry-run

# tests
ruff check . && python -m pytest tests/ -q
```

In production the transform job reads the lake straight from the Hub instead of a local
directory — `--vars '{"bronze_root": "hf://datasets/swadhinbiswas/TerraSentinel"}'` — and CI
always runs the local form, so the same SQL is exercised with and without the network.

dbt is invoked from the repository root with `--project-dir transform`, which keeps every
relative path (and the DuckDB file) cwd-stable.

`--plan` before `--backfill` is the intended workflow: a two-year FIRMS backfill is ~880
requests, all three sources ~900.

## Design decisions

Each of these is written up in `docs/adr/`:

- [DuckDB over Spark](docs/adr/0001-duckdb-over-spark.md) — no warehouse, no cluster
- [Turso over Postgres](docs/adr/0002-turso-over-postgres.md) — edge reads, batched writes
- [H3 over raw lat/lon](docs/adr/0003-h3-over-raw-latlon.md) — bucketing at ingestion
- [Astro + Cloudflare over Streamlit](docs/adr/0004-astro-cloudflare-over-streamlit.md)
- [GEE aggregation grain](docs/adr/0005-gee-aggregation-grain.md) — why Sentinel is not
  bucketed per-pixel
- [NOAA source selection](docs/adr/0006-noaa-opendap-over-erddap.md) — what the free,
  reachable archives actually are
- [Bronze glob selection](docs/adr/0007-bronze-glob-selection.md) — why the listing happens
  outside dbt
- [Mirroring bronze locally](docs/adr/0009-mirror-bronze-locally.md) — why the transform
  does not read `hf://` directly

Two things worth knowing before reading the code:

**Every collector is a template method.** `fetch()` is the only source-specific part; retry
with full jitter, a per-source circuit breaker, Pandera validation at both ingress and
egress, H3 indexing, provenance columns, and partitioned writes are inherited. A collector
that hangs on a dead API is a bug in the base class, not in each collector.

**Bronze is append-only and source-shaped.** Deduplication and schema unification happen in
the staging models. That means a re-run is always safe, and an upstream column rename shows
up as a validation failure at ingestion rather than as a null-filled dashboard.

## Data attribution

Fire data: NASA FIRMS (MODIS and VIIRS active fire products). Imagery: Copernicus Sentinel-2
and Sentinel-1 (ESA), processed in Google Earth Engine. Ocean and ice data: NOAA OISST v2.1
and NSIDC Sea Ice Index (v4.0). Attribution strings live in `collectors/config.py` and are
served to the dashboard from the `sources` table, so the footer cannot drift out of date.
