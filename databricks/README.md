# TerraSentinel on Databricks

This folder is the **Databricks-native half of a hybrid pipeline** — the ADR
below records why the split is drawn where it is. The full deployment
guideline lives in [`databricks/docs/`](docs/GUIDELINE.md), which is
deliberately **git-ignored** (it tracks CLI/bundle revisions and changes more
often than the code).

## Contents

| Path | Purpose |
| --- | --- |
| `../databricks.yml` | Asset Bundle root (bundle name, variables, presets, targets) |
| `resources/jobs.yml` | The Workflows DAG: 5 jobs, Quartz crons mirroring GitHub |
| `jobs/*.py` | Task entry scripts (`spark_python_task`, self-contained) |
| `sql/gold_ddl.sql` | **Generated** UC DDL + MERGE — never hand-edit |
| `sql/bootstrap.sql` | One-time admin step: catalog, schema, volume |
| `../tools/export_uc_ddl.py` | Generator with `--check` freshness gate (CI) |
| `../transform/profiles.yml` | `databricks` dbt target (DuckDB file on the Volume) |
| `../collectors/config.py` | `get_secret`: env first, secret scope second |

## ADR-1: hybrid v1 — Databricks *adds*, GitHub Actions *stays*

| Decision | Choice | Why |
| --- | --- | --- |
| Pipeline ownership | GitHub Actions (free-tier) keeps bronze collection, HF mirroring, dbt, Turso sync; Databricks runs the same DAG *paused* by default | Deploying must be side-effect-free; handover is a documented one-line flip (`presets.trigger_pause_status`), never an implicit second scheduler |
| Bundle layout | Bundle root = repo root (`databricks.yml` at top level, `include: databricks/resources/*.yml`) | Task scripts need `collectors/`, `tools/`, `ml/`, `transform/` on `sys.path`; syncing one subtree would fork the codebase |
| Job entry | `spark_python_task` scripts (`jobs/*.py`), not wheels or notebooks | No `setup.py`/hatchling build step, no editable install on the cluster; each script resolves the repo root from `--repo-dir` / `TERRASENTINEL_ROOT` and imports the same modules CI tests |
| Gold publishing | DuckDB file on a Unity Catalog **Volume** → generated `CREATE` + key-matched `MERGE` into UC tables (`sql/gold_ddl.sql`) | DDL types are read from the dbt-built schema (never a hand-maintained copy); `MERGE` keys and the ML-owned column exclusion come from `GOLD_TABLES` / `ML_OWNED_COLUMNS`, the same structures the Turso push uses — one contract, two sinks |
| UC provisioning | One-time `sql/bootstrap.sql` (catalog/schema/volume), not bundle UC resources | Catalog creation needs admin rights; `dev_` name prefixing would break the paths jobs write to; `IF NOT EXISTS` SQL is idempotent across targets |
| dbt on Databricks | New `databricks` **target** in the same profile (still DuckDB + httpfs), `dbt-databricks` deferred | v1 keeps one transform codebase byte-identical on both runners; the Delta path is already covered by the UC merge, so a warehouse/dbt-databricks port buys nothing yet |
| Secrets | Task `env` carries only non-secrets (`DATABRICKS_SECRET_SCOPE`, `HF_NAMESPACE`, paths); `collectors.config.get_secret` falls back to the secret scope via dbutils | Env-first keeps GitHub/local `.env` behaviour untouched (existing tests unchanged); scope fallback needs no secret references in YAML, so the bundle never holds credential-shaped keys — asserted by tests |
| Schedules | One job per cadence, Quartz crons converted from SourceSpec/GitHub | A tested converter (`tests/test_databricks.py`) makes drift between the two runners a CI failure, not an incident |
| ML registry | `--mlflow-uri databricks` on the train task + publish to the Hub as before | Workspace MLflow is the Databricks-native registry; the Hub stays the runner-neutral artifact both paths can load |

## Freshness gates (CI)

```bash
python -m tools.export_seeds --check       # dbt seeds vs Python registries
python -m tools.export_uc_ddl --check      # UC DDL/MERGE vs GOLD_TABLES + dbt schema
ruff check .
python -m pytest tests/ -q
```

## Quick start

```bash
databricks bundle validate -t dev
databricks bundle deploy  -t dev     # schedules stay PAUSED (preset)
```

Secrets, UC grants, job-by-job reference, MLflow notes and the handover
procedure: see [`docs/GUIDELINE.md`](docs/GUIDELINE.md).
