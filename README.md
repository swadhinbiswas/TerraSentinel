# <img src="serving/dashboard/public/favicon.svg" alt="" width="44" height="44"> TerraSentinel

Anomaly detection on free public satellite and sensor data (wildfire, deforestation,
glacier and ice melt), running end to end on free tiers. Ingestion, a versioned data lake,
SQL transforms, unsupervised ML, and an edge-served dashboard.

**Cost: $0.** GitHub Actions does the compute, Hugging Face Hub stores the lake and the
model registry, Turso serves the gold tables, Cloudflare Pages serves the dashboard.

![The TerraSentinel overview page: the anomaly map over Iberia with its H3 cells, the daily
fire detections chart, and KPI cards reporting 268,543 detections and 32 flagged days.](docs/dashboard.png)

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Historical backfill (FIRMS, Sentinel, NOAA/NSIDC) → HF bronze | NOAA/NSIDC + FIRMS **live** on schedule; GEE awaiting service-account key |
| 1 | FIRMS → bronze → dbt staging → Turso → one API route | collectors + staging + sync done; API routes shipped with phase 6 |
| 2 | Sentinel (GEE) + NOAA/NSIDC collectors, unified silver contract | collectors done |
| 3 | dbt staging/intermediate/gold + automated Turso sync | **done**: 117 nodes, all tests green |
| 4 | Feature table, IsolationForest, MLflow, HF model registry, batch scoring | **done** |
| 5 | Known-events validation + synthetic anomaly injection in CI | **done** |
| 6 | Astro + shadcn/ui dashboard on Cloudflare Pages | **deployed**: https://terrasentinel-dashboard.pages.dev |
| 7 | Evidently drift check → auto-retrain, alerting on every workflow | alerting done; drift pending |
| 8 | Architecture decision records | written, folded into Design decisions below |
| 9 | ENTSO-E day-ahead price + actual load (4th source) | collector **done**; bronze only, no dbt staging model |
| 10 | Databricks hybrid path (Asset Bundle, Unity Catalog, Workflows, MLflow) | scaffolded + tested; free-tier path stays primary |

`592` Python unit tests, `145` dashboard tests, a full dbt build, and a type-checked
dashboard build, all offline. The
transform layer runs against a synthetic lake with deliberately injected anomalies, and CI
asserts the gold marts actually flag them; see "Proving it works" below.

**Pipeline health:** the scheduled runs are green: collect writes hundreds of rows a run,
the transform publishes tens of thousands, and every dashboard API route answers from
Turso. `collect sentinel` skips with a notice until the `GEE_SERVICE_ACCOUNT_JSON` GitHub
secret is provisioned, so a red run always means a real breakage.

**Live:** [terrasentinel-dashboard.pages.dev](https://terrasentinel-dashboard.pages.dev).
9 pages on Cloudflare's edge that read Turso directly from workerd, with no backend service
in between.

**Serving database live:** 76,607 rows synced into Turso
(`terra-globalopenresearch`, ap-south-1): four gold marts plus 1,462 model predictions.
The dashboard reads it at sub-10 ms through Pages Functions.

**Model published:** [`swadhinbiswas/TerraSentinel-models`](https://huggingface.co/swadhinbiswas/TerraSentinel-models),
an IsolationForest on 27 strictly causal features traced to bronze commit `445d10f3`. The
card measures instead of asserting: model flags are compared against the *independent*
median/MAD rule in `gold_fire_anomalies.is_anomaly`, and precision, reference recall and
Jaccard are printed for that comparison. The "adds **no information**" limitation appears
only when that agreement is measured as total (precision and Jaccard both ≥ 0.99);
otherwise the card reports the real overlap and names the reference rule. When the
limitation holds, the card gives the reason too: the top 2.5% of fire days run ~20× the
rest, so they are trivially separable and every method finds the same ones.

**Live as of this run:** 55,677 real NOAA/NSIDC rows landed in
[`swadhinbiswas/TerraSentinel`](https://huggingface.co/datasets/swadhinbiswas/TerraSentinel)
in a single commit, and the transform read them back off the Hub. Arctic sea-ice extent in
June 2026 is running ~1.3 × 10⁶ km² below the 1981-2010 normal (z ≈ −3.3).

## Storage layout

Two repos, both under the same namespace:

| Repo | Type | Holds |
|---|---|---|
| `<namespace>/TerraSentinel` | dataset | the bronze lake: `firms/`, `sentinel/`, `noaa_nsidc/`, `entsoe/`, `backfill/`, `ops/` |
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
pool. A scheduled job will hit HTTP 429 on the repo tree API.
`tools/configure_duckdb_secret` registers it, deliberately *outside* dbt so the token never
reaches compiled SQL or an uploaded artifact.

## Architecture

![TerraSentinel architecture: four public sources feed cron-scheduled collectors into a
versioned Hugging Face bronze lake, which dbt and DuckDB turn into seven gold marts synced
to Turso, while an ML branch trains an IsolationForest and batch scores predictions back
into Turso; Astro API routes on Cloudflare Pages serve the dashboard. A dashed Databricks
path mirrors the lake as a paused hybrid second path.](docs/architecture.svg)

**No live Python inference runs on the request path.** Batch scoring applies the
registered model to the latest gold features and writes `anomaly_score` into Turso as part
of the scheduled job; the dashboard's API routes only ever do a fast SQL read, which keeps
the edge functions inside Cloudflare's CPU budget and the dashboard fast.

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
combined "all ice" mart meant a missing Sentinel source also removed the sea-ice results,
data that was present, healthy, and unrelated to Sentinel. The same applied to a single
combined map mart. Splitting them means an outage can only remove its own table, and the
dashboard reads whichever exist.

The transform mirrors the lake locally first (`tools/sync_bronze.py`: one listing
plus parallel CDN fetches, 102 files / 8.8 MB / 18 s measured), then builds entirely
off local disk. DuckDB's `hf://` filesystem re-lists the repo tree on every query that
touches a remote path, which is fine for a notebook and fatal for a scheduled build:
with staging as views, ~50 dbt tests meant 50+ tree listings against a
1,000-per-5-minutes API quota. After mirroring, a full 117-node build makes zero API
calls and runs in ~4 seconds.

*Which* globs dbt reads is still decided outside dbt by `tools/bronze_manifest.py`. That indirection exists
because both simpler options failed against real data: a two-glob list
(`<source>/**` plus `backfill/<source>/**`) dies the moment a prefix is missing, which is
exactly the state after a backfill, and a single `**` pattern works over `hf://` but not on a
local filesystem. So the listing happens once, authenticated and retried, and dbt receives
only globs known to match. A source with no files at all fails its own branch with a relation
named `bronze_missing_for__<source>__run_backfill_historical_then_rerun`.

Partitioning is switched off when reading: live and backfill landings have different hive
key sets (only live paths carry `day=`) and DuckDB refuses to read both in one call. Nothing is
lost, because `region_id` is a real column on every row, which also decouples the models from
the physical layout entirely.

## Anomaly definitions

Every mart states its baseline, because "anomaly" is the whole product:

| Mart | Signal | Compared against |
|---|---|---|
| `gold_fire_anomalies` | daily detection count | median of the same ±15 days across years, scaled MAD as the yardstick |
| `gold_deforestation_index` | monthly NDVI | the same month a year earlier; z-score against that region's own change distribution |
| `gold_ice_extent_trends` (extent) | daily sea-ice extent | the published NSIDC 1981-2010 per-day normal and its standard deviation |
| `gold_glacier_backscatter` (SAR) | monthly backscatter | year-over-year change distribution (no climatology exists; `baseline_source` says so) |

Two choices are worth calling out. **Median/MAD, not mean/stddev**, for fire: a large fire
sits inside its own baseline window, and with mean/stddev it inflates the very yardstick it is
measured against, partly hiding itself. **A ±15-day pooled window, not "same date last
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
  the day it scores. The statistical `zscore` is excluded as a feature because it is
  computed from the whole history including the future; using it would make the comparison
  between model and rule circular.
- **The score is a percentile**, ranked against the training distribution stored in the
  bundle, so "0.98" means the same thing at serving time as it did at training time.
- **Evaluation reports what is measurable without labels**: distribution shape, separation
  from the rest of the data, agreement with the rule (as a weak comparator), and the
  documented real events. Never accuracy or F1.

## The dashboard

```
serving/dashboard/   Astro + React islands + Tailwind v4 + shadcn/ui, on Cloudflare Pages
  src/lib/turso.ts        libSQL client — server-side ONLY, never imported by an island
  src/lib/queries.ts      every read the dashboard performs, in one place
  src/lib/registry.ts     the data catalog, the explorer's table allowlist, source→mart map
  src/lib/sql-guard.ts    SELECT-only guard for the console
  src/lib/flow.ts         stage specs and the state rules behind the pipeline view
  src/lib/periods.ts      the map's window presets, and which layers each suits
  src/lib/utils.ts        the oklch→hex conversion both canvas renderers need
  src/lib/svg-chart.ts    the scale and path arithmetic the charts are built from
  src/lib/theme.ts        useTheme, with no mount-time setState
  src/components/map/     webgl probe · shared geometry · Canvas 2D fallback renderer
  src/components/flow/    the live pipeline rail
  src/pages/api/          health · anomalies · ice · map · flow · query · explorer · ops
```

Nine pages: Overview · Pipeline · Analysis · Stories · Catalog · Explorer · SQL · Ops · Docs.

### The pipeline view

`/pipeline` is the page that answers "is this still working". Six stages — collect, store,
transform, train, score, serve — each with a state derived from `pipeline_runs`:

| State | Means |
|---|---|
| `live` | last run succeeded and is inside the stage's freshness budget |
| `overdue` | last run succeeded but is past the budget for that stage's cadence |
| `failed` | the most recent run did not succeed, whatever its age |
| `unrecorded` | the workflow writes a run row on every execution, and there are none |
| `external` | the stage has no run of its own (serving: the dashboard answering is the evidence) |

The distinction that matters is `unrecorded`. A stage that has never run and a stage whose
run history is missing look identical in the database, and the default rendering — green —
reads as healthy in both cases. `unrecorded` renders as a gap and names the reason instead.
The same rule governs everything else on the site: a mart that is absent is reported as
absent, a source without credentials is reported as blocked, and a stage that is late says
how late. Nothing is inferred from a neighbouring stage's health.

Each source carries two independent badges, because collecting and serving are different
stages and only one of them is a promise to a reader: whether its credentials are present,
and whether any mart is built from it. **ENTSO-E is the second badge that says "not on a
page".** It is collected daily with a valid key and there is no gold mart from it, so
nothing can show it — previously the page said nothing either way, which is how a
collected-but-unserved source reads as a served one. The `sourceIds` field on the registry
is the single declaration behind that, inverted into `servedBy`; the mart itself is a dbt
change and is listed as phase 9 in the status table above.

### The map

Two renderers, chosen by a probe rather than by assumption.

**MapLibre** when the browser can actually give it a WebGL context. **A Canvas 2D renderer**
when it cannot, drawing the same H3 boundaries computed in the browser by h3-js.

This exists because `maplibre-gl` calls `canvas.getContext("webgl2")` in its `Map`
constructor and throws when it returns null, unguarded and with no error boundary — so a
browser with WebGL disabled or blocklisted took the whole page's map panel down. The obvious
fix, testing `"WebGL2RenderingContext" in window`, does not work: that is still `true` in
exactly the browsers that fail. The probe has to ask for a context and make it answer a
call, because a blocklisted driver can hand out a context whose every method throws.

The probe is eight cases in `webgl.test.ts` and they are worth reading, because the
interesting one is the fifth: creating a context is not evidence that it works.

**The map's data is fetched, not embedded.** The overview was 1.75 MB, of which 1,607,256
bytes were a single island props blob: 4,392 cells, devalue-encoded into the markup. The
island re-fetched on mount anyway, and `client:only` renders an empty div either way, so
the seed was covering a gap of a few hundred milliseconds at the cost of a megabyte. The
page now passes only the window's summary and the island shows a skeleton until its own
response lands. That is the one regression, and it is deliberate: a loading state that
says "loading" is worth more than a first paint that arrives a round trip earlier.

**MapLibre is not in the island bundle.** It is loaded with a dynamic `import()` *after*
the probe passes. Before, the island was 1,292 kB / 358 kB gzipped and roughly two thirds of
that was a renderer that the browsers reaching the fallback branch can never construct. It
is now 231 kB / 72 kB gzipped, with MapLibre in a separate 1,053 kB / 285 kB chunk that
only a machine which can run it ever fetches — an 80% cut for exactly the population that
was crashing. `Popup` is passed into the one module-scope function that needs it, because
a function outside the component cannot close over a module that is not there yet.

`/api/map` is the heaviest response the site serves, so it carries a strong ETag. A full
season of daily cells is ~1.2 MB of JSON, and the underlying data changes a few times a
day, so without a validator every revalidation after the 300s window re-downloads all of
it. With one, the second visit onward gets a 304 and 0 bytes. The browser revalidates
transparently; there is no client-side code involved.

**The window list is layer-aware.** The presets are anchored to measured fire events, and
they were offered against the SST layer too — which starts in June 2026 and has no rows
before then. The result was an empty map, and an empty state that explained the gap by
naming the Earth Engine backfill, which has nothing to do with sea-surface temperature. A
table of which layers each window suits (`PERIOD_LAYERS`) fixes the dropdown, the hints are
resolved per layer so a shared window cannot describe the other layer's data, and the empty
state now states one cause it can prove from the response rather than two that disagree.

**The map's state is addressable.** Layer, window, region and mode live in the query
string, validated against the option lists on read (the query string is user-editable, and
a bad `layer=foo` would otherwise reach the API). A finding can be linked to, and the back
button leaves the page rather than walking through every change made to it.

**The densest cells are reachable from the keyboard.** The map is a canvas in both
renderers, so nothing drawn in it is focusable. The eight densest cells are also exposed as
a real list of buttons; tabbing through them moves the camera, which is also the most
direct way to check that the numbers on screen are the numbers in the data.

### UI decisions that were forced by bugs

Four things about this UI are the way they are because the obvious version did not work:

**The charts are hand-rolled SVG, not a charting library.** Recharts failed three separate
ways: v2 predates React 19, v3 mis-measured its container (rendering squashed into a
corner), and an island that fails to hydrate renders *nothing*. For one area series, one
bar series and two lines, computed SVG paths render on the server, so the first paint
contains the chart, a JS failure cannot blank the panel, and 395 KB of gzipped dependency
disappeared. Tooltips are `<title>` elements: native, keyboard-accessible, no JavaScript.

**The map attaches on `style.load`, not `isStyleLoaded()`.** `isStyleLoaded()` asks about
*tiles*, and the data layers are added after it returns — so the guard passed, the layers
were never added, and the map rendered 0 of its 4,392 features while the network tab showed
every one of them arriving. This is the single most expensive bug in the dashboard, and
nothing about it was visible from the outside except an empty map.

**The camera never animates.** `fitBounds` with a duration is a *motion*, and a motion that
does not run leaves the camera on its default centre. Framing is instantaneous and driven
by a `ResizeObserver` on the container, because the container has no size until the
stylesheet gives the grid its columns.

**The colour ramp is converted from the live tokens, not hardcoded.** `rampHex()` reads
`--color-i1`..`--color-i5` off the document and does the oklch→sRGB transform itself. The
hardcoded hexes had drifted from the CSS, and on the light theme — where the ramp runs light
to dark — that meant the top step was painted near-white on white. A hand-rolled colour
transform is exactly the kind of code that is wrong quietly, so it is verified against an
independent reference implementation in `utils.test.ts`, for both themes, plus a contrast
assertion that fails if any step lands within 0.05 relative luminance of the page
background.

**The map uses dual encoding (dots *and* hexagons).** Measured: an H3 res-7 cell is ~1.1 km
across, which is 0.23 px at zoom 5 and still under 4 px at zoom 9. Drawing only polygons
means the map looks empty at every zoom a region-wide view needs.

**Colour is always redundant.** A red-to-green severity scale is invisible to the ~8% of
men with red-green deficiency, and red-green is the obvious choice for severity.
`--color-i1`..`--color-i5` run from a deep ember to near-white in luminance, and every
value is also labelled in text.

### Two things that had to be `.astro` and not `.tsx`

`PageHeader` and `SectionLabel` are Astro components, and they have to stay that way. Astro
passes `.astro` children to a React component as a **slot function**, not as rendered
output — so a React component that renders `children` inside its own render pass triggers a
nested-update warning and skips commits. The fix is not a `useEffect` around it; the fix is
that these two components are not React.

`useTheme` reads its initial value in the `useState` initialiser rather than in an effect,
because a `setState` that lands during mount interrupts a sibling island's render.

### shadcn/ui

`components.json` is present and the registry components are aliased onto this project's
palette through `@theme inline` in `global.css` — the token names are mapped *onto* the
existing colours rather than a second palette being introduced, so a registry component
lands in the design language instead of replacing it.

`alert` and `table-frame` earn their place: the degraded-panels banner was hand-rolled with
an inline `color-mix` in three separate pages, and a sticky `<th>` is decoration without a
scroll container. `accordion`, `dropdown-menu`, `scroll-area`, `toggle-group` and `toggle`
were installed, found to have no job here, and removed — `<details>` needs no JavaScript,
the nav is plain links, tables scroll natively, and the period and region pickers are
native `<select>` elements on purpose.

### The dashboard's own tests

```bash
cd serving/dashboard
npm test          # vitest, no credentials, no network
```

145 cases over the parts where a wrong answer is quietly wrong rather than loudly broken:
the SQL deny-list (the one place a wrong answer is a security problem), the oklch→hex
conversion, the pipeline's stage-state rules, the WebGL probe, the map projection and cell
readout, the chart arithmetic, and the pipeline's relative-time formatting. The suite runs in
CI before the build.

They are not ceremonial. Writing them found two real bugs: `mercatorY` leaked outside the
unit square by ~1e-9 at the poles, which shifts every projected point through the canvas fit;
and the boundary cache handed out a live reference, so a caller mutating a ring corrupted
every subsequent paint. The readout's separation of database strings from markup is asserted
for the same reason — it was once a `setHTML()` call with a cell's `region_id` in it.

### Degradation and access

Seven API routes: every one is read-only, and everything it touches is precomputed. Nothing
in the built-in views aggregates, loads a model, or infers; that work happens in scheduled
GitHub Actions jobs, because Pages Functions have a short CPU budget on the free tier.

- **Server-rendered first paint.** `index.astro` queries Turso during the request, so the
  HTML arrives with real numbers. Only the map is a `client:only` island.
- **A missing panel is named, not hidden.** Three marts depend on Sentinel data that is not
  backfilled; `tableExists` is checked and a missing table empties its own panel, and the
  banner at the top of the page lists exactly which.
- **The activity strip states its own basis.** It counts every cell in the window while the
  map draws only the densest 6,000, and when the cap bites the strip says so — otherwise a
  bar reads as "these are the cells on the map" when some were never sent.
- **Focus rings are never suppressed**, and `prefers-reduced-motion` is honoured rather than
  overridden.

Turso credentials are Cloudflare Pages environment variables, read server-side. The browser
only ever talks to the site's own `/api/*`.

### Deploying

```bash
cd serving/dashboard
npm run build
npx wrangler pages deploy dist --project-name=terrasentinel-dashboard --branch=main
```

Three things that must be right, all of which cost time to discover:

- **`compatibility_flags = ["nodejs_compat"]`** in `wrangler.toml`. Astro's image service
  pulls in `sharp`, which requires node builtins even though this dashboard renders no
  images. Without the flag, the worker fails to bundle.
- **React 18, not 19.** React 19's server renderer uses `MessageChannel`, which workerd does
  not provide, so the worker dies at startup with `ReferenceError: MessageChannel is not
  defined`. React 18 avoids that code path.
- **`astro build` in CI, not just `astro check`.** `check` passes builds that then fail.
  A type error that stops a build is cheap to find locally; one that only appears at
  `wrangler pages dev` is not, so the local check of the *built output* is the only way to
  catch the runtime items above.

## Proving it works

An unsupervised model can always claim to work. So the pipeline ships a falsifiable check:

```bash
python -m tools.synthetic_bronze --out data/bronze --clean --inject-anomaly
dbt build --project-dir transform --profiles-dir transform --vars '{"bronze_root": "data/bronze"}'
python -m tools.assert_anomalies_detected --duckdb-path transform/terrasentinel.duckdb
```

The generator plants one grossly obvious event per anomaly type (a 60-detection fire day, a
0.35 NDVI collapse, a 1.8 × 10⁶ km² sea-ice excursion, a 3 °C marine heatwave). The checker
asserts each is flagged and that the overall flag rate stays under 2%: a degenerate baseline
that flags everything would sail past a naive sensitivity check. CI runs
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
databricks/      hybrid second path: Asset Bundle, Workflows, UC DDL (paused)
docs/            architecture diagram
pandera_schemas/ the data contracts between layers
tools/           synthetic lake generator, seed export, anomaly assertions
tests/           592 Python tests plus a full dbt build, all offline
```

`serving/dashboard/` carries its own suite — `npm test`, 145 cases, no credentials — for the
reason the layout cannot express: the dashboard reads from a live database at request time,
so the parts worth testing are the pure functions at its edge rather than its pages.

## Setup

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
# install the pinned dependency set (requirements/*.lock) plus this package
uv pip install --no-deps -r requirements/ci.lock -e .
cp .env.example .env                # fill in credentials (never committed)
```

Python 3.12 is pinned: GEE and torch lag newer releases. Installing from a lock is the
default because it is reproducible: `uv pip install -e ".[dev]"` still works but resolves
whatever PyPI has *today*. Recompile the locks whenever `pyproject.toml` changes;
`requirements/README.md` has the six commands, and `tests/test_workflows.py` fails if CI
installs from anywhere else.

Optional extras: `[gee]` Sentinel via Earth Engine, `[noaa]` OPeNDAP, `[transform]` dbt+DuckDB,
`[ml]` scikit-learn, `[drift]` Evidently.

### Credentials

| Variable | Needed for | Where to get it |
|---|---|---|
| `HF_TOKEN`, `HF_NAMESPACE`, `HF_PROJECT` | bronze/silver datasets, model registry | huggingface.co/settings/tokens |
| `FIRMS_MAP_KEY` | fire detections | firms.modaps.eosdis.nasa.gov/api/map/key |
| `GEE_SERVICE_ACCOUNT_JSON` / `_EMAIL` / `GEE_PROJECT` | Sentinel-2/-1 | free for research; register a service account for Earth Engine |
| `TURSO_DATABASE_URL`, `TURSO_TOKEN_RO` | gold/serving DB (read-only token) | turso.tech free tier |
| `TURSO_AUTH_TOKEN` | same DB, write scope: pipeline upserts; fallback for the dashboard | turso.tech free tier |
| `ALERT_WEBHOOK_URL` | failure alerts | Slack or Discord incoming webhook |

NOAA and NSIDC need no credentials. Nothing reads a secret from anywhere but the
environment, and everything that formats text for a human passes through `ops/redact.py`
first, URLs included, because FIRMS puts its key in the request *path*.

**Give the dashboard a read-only token.** Every statement the serving layer issues is a
`SELECT`, so it has no business holding write scope: with a write token, a bug in the SQL
console's guard would have write consequences.

```bash
turso db tokens create <db-name> --read-only
# → set the value as TURSO_TOKEN_RO in Cloudflare Pages → Environment variables
#   (and in serving/dashboard/.dev.vars for local dev), then revoke the write token:
turso db tokens revoke <db-name> <token-name>
```

`turso()` prefers `TURSO_TOKEN_RO` and falls back to `TURSO_AUTH_TOKEN`, so rotation is an
environment change with no deploy and no code path change; the SQL console reports the
scope of whichever token it is actually using.

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

# the dashboard's own suite — no credentials, no network
cd serving/dashboard && npm ci && npm test
```

In production the transform job reads the lake straight from the Hub instead of a local
directory (`--vars '{"bronze_root": "hf://datasets/swadhinbiswas/TerraSentinel"}'`); CI
always runs the local form, so the same SQL is exercised with and without the network.

dbt is invoked from the repository root with `--project-dir transform`, which keeps every
relative path (and the DuckDB file) cwd-stable.

`--plan` before `--backfill` is the intended workflow: a two-year FIRMS backfill is ~880
requests, all four sources ~1,160.

## Design decisions

Eleven choices here are not self-evident from the code. Each one names the measurement that
forced it, because the reasoning is the part worth reading.

**DuckDB over Spark.** Two years of four sources is 10⁵-10⁶ numeric rows. A cluster adds
cost and operational surface for no capability, and DuckDB reads the lake over HTTP with no
staging step. Its limits are real (a process-per-job model, no libSQL adapter) and both
are handled explicitly rather than hidden.

**Turso over Postgres.** Pages Functions run in `workerd`, which cannot open a raw TCP
connection. Reaching Postgres would need Hyperdrive or a proxy service on the request path.
libSQL's HTTP protocol works from `workerd`, from CPython, and from CI with the same request
shape: one transport and one typing rule set across the sync job and the serving layer.

**H3 over raw lat/lon.** Grouping becomes string equality rather than a spatial join,
resolutions nest for free, and proximity is a cheap k-ring. Resolution is chosen per source
rather than uniformly, because the right bucket size differs by an order of magnitude
between a 375 m fire detection and a 0.25° ocean grid.

**Median and MAD over mean and standard deviation.** A large fire sits inside its own
±15-day baseline window and would inflate the yardstick it is measured against, partly
hiding itself. Measured on the same event: z 7.2 with mean/stddev versus 16.9 with
median/MAD. And a ±15-day pooled window rather than "same date last year", because with two
years of history a same-date baseline has a sample size of two.

**Astro + Cloudflare over Streamlit.** Streamlit is the fastest route to a demo and the
wrong shape: a Python server on the request path, limited layout control, no edge caching.
Astro ships static HTML with two interactive islands, and Pages Functions read Turso
directly with no backend service.

**Two grains for Sentinel, not per-pixel H3.** Measured: bucketing Sentinel at res 7 means
203,485 cells for Iberia and 677,760 for Norway: 200k+ polygons per `reduceRegions` call,
which no free Earth Engine quota absorbs. A region-grain series (one request for an entire
multi-year backfill) plus a res-5 change map answers the same questions at a cost that runs.

**OPeNDAP from NOAA PSL, not the obvious archive.** The documented ERDDAP endpoint redirects
to a host that times out, and NCEI's OISST copy turned out to be a stale 2002-2011 slice.
NSIDC had also moved v3.0 → v4.0. All three findings came from probing the archives rather
than reading about them.

**Bronze glob selection outside dbt.** Two globs (`<source>/**` plus `backfill/<source>/**`)
die the moment one prefix is missing, which is exactly the state after a backfill. A single
leading `**` works over `hf://` but not on a local filesystem. So the listing happens once,
authenticated, and dbt receives only globs known to match.

**FIRMS product selection by window age.** Standard processing is published ~3 months behind
real time and near-real-time is retained ~3 months, so choosing products by "is this a
backfill?" returns silently empty data for the most recent quarter. Measured against the
live API, with a fallback through the overlap.

**Mirror the bronze lake before transforming.** DuckDB's `hf://` filesystem re-lists the repo
tree on every query, so a build made dozens of API calls against a 1000-per-5-minutes quota.
One listing plus parallel CDN fetches turns that into zero, and the build runs in seconds.

**Databricks as a hybrid second path, not a migration.** The free-tier path (GitHub Actions →
Cloudflare → HF → Turso) stays the primary and the tested one; Databricks only *adds* an
Asset Bundle with a paused-by-default Workflows DAG, a `databricks` dbt target writing to a
UC Volume, and MLflow registration. What forced "generated, not hand-written" for the Unity
Catalog DDL and MERGE statements is the CI gate `python -m tools.export_uc_ddl --check`: the
gold schema has exactly one source of truth (`GOLD_TABLES`), and any drift between a mart
model and the committed `databricks/sql/gold_ddl.sql` fails the build before a Databricks
job could ever see it. Handover is one commit: unpause the bundle jobs, pause their GitHub
twins. See `databricks/README.md` for the ADR.

## Data attribution

Fire data: NASA FIRMS (MODIS and VIIRS active fire products). Imagery: Copernicus Sentinel-2
and Sentinel-1 (ESA), processed in Google Earth Engine. Ocean and ice data: NOAA OISST v2.1
and NSIDC Sea Ice Index (v4.0). Energy market data: ENTSO-E Transparency Platform (day-ahead
prices A44, actual total load A65). Attribution strings live in `collectors/config.py` and are
served to the dashboard from the `sources` table, so the footer cannot drift out of date.
