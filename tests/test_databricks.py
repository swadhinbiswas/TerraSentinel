"""Databricks bundle contracts.

The bundle is the Databricks-native half of a hybrid pipeline: it must mirror
GitHub Actions rather than drift from it. These tests enforce that mirror
without needing a Databricks CLI or a workspace:

* bundle shape (engine, presets that keep schedules paused, include glob);
* every job is scheduled, and every cron equals its SourceSpec / GitHub cron
  translated to Quartz (the converter lives here, next to the assertion);
* pypi libraries on tasks are pinned, and each pin appears verbatim in a
  reviewed lockfile (a lock bump must not silently desync the bundle);
* the generated ``databricks/sql/gold_ddl.sql`` covers every ``GOLD_TABLES``
  mart with the right merge keys and never writes the ML-owned columns;
* dbt's ``databricks`` target differs from ``dev`` only in where the DuckDB
  file lives, and that location agrees with the bundle's variables;
* credentials are resolved env-first with a secret-scope fallback, and no
  secret-shaped key ever appears in the bundle YAML;
* ``databricks/docs/`` (the full guideline) stays out of git.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from collectors.config import SOURCES
from ops.redact import SECRET_ENV_VARS
from sync.push_gold_to_turso import GOLD_TABLES, ML_OWNED_COLUMNS

REPO = Path(__file__).resolve().parents[1]
BUNDLE = yaml.safe_load((REPO / "databricks.yml").read_text(encoding="utf-8"))
JOBS = yaml.safe_load((REPO / "databricks/resources" / "jobs.yml").read_text(encoding="utf-8"))
SQL_TEXT = (REPO / "databricks" / "sql" / "gold_ddl.sql").read_text(encoding="utf-8")

#: GitHub day-of-week numbers (0=SUN..6=SAT) -> Quartz day names.
GH_DOW = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


def to_quartz(cron: str) -> str:
    """Translate a GitHub 5-field cron to the Quartz form Databricks expects.

    Quartz needs a leading seconds field and *exactly one* of day-of-month /
    day-of-week set to ``?``. Convention used across this repo (pinned by
    ``test_quartz_conversion_examples``): an unrestricted weekday puts ``?``
    on day-of-week (day-of-month keeps ``*``); a specific weekday puts ``?``
    on day-of-month. Every cron here restricts day-of-month only via ``*``,
    so nothing else needs translating.
    """
    fields = cron.split()
    assert len(fields) == 5, f"expected 5-field GitHub cron, got {cron!r}"
    minute, hour, dom, month, dow = fields
    assert dom == "*", f"cron {cron!r} restricts day-of-month; extend to_quartz first"
    if dow == "*":
        return f"0 {minute} {hour} * {month} ?"
    return f"0 {minute} {hour} ? {month} {GH_DOW[int(dow)]}"


def scheduled_crons(workflow: Path) -> list[str]:
    """The ``schedule`` crons of a GitHub workflow file (PyYAML parses `on` as True)."""
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    on = doc.get("on") or doc.get(True)
    return [entry["cron"] for entry in on["schedule"]]


def job_parameters(job: dict, task_key: str) -> list[str]:
    for task in job["tasks"]:
        if task["task_key"] == task_key:
            return task["spark_python_task"]["parameters"]
    raise AssertionError(f"task {task_key!r} not found")


# --------------------------------------------------------------------------
# Bundle root
# --------------------------------------------------------------------------


def test_bundle_root_shape() -> None:
    assert BUNDLE["bundle"]["name"] == "terrasentinel"
    # Explicit direct engine: older CLIs must fail loudly, not silently fall
    # back to Terraform (which cannot deploy the UC resources).
    assert BUNDLE["bundle"]["engine"] == "direct"

    matched = sorted(REPO.glob(BUNDLE["include"][0]))
    assert matched, f"bundle include {BUNDLE['include'][0]!r} matches nothing"
    assert REPO / "databricks/resources/jobs.yml" in matched

    # Deploying must never start a second scheduler — schedules stay paused
    # until a human opts in (docs/GUIDELINE.md §7 handover).
    assert BUNDLE["presets"]["trigger_pause_status"] == "PAUSED"
    assert BUNDLE["presets"]["jobs_max_concurrent_runs"] == 1

    variables = BUNDLE["variables"]
    for name in ("spark_version", "node_type_id", "hf_namespace", "uc_catalog", "uc_schema", "secret_scope"):
        assert variables[name]["default"], f"variable {name} needs a default (bundle must validate offline)"

    targets = BUNDLE["targets"]
    assert targets["dev"]["mode"] == "development"
    assert targets["dev"].get("default") is True
    assert targets["prod"]["mode"] == "production"


def test_quartz_conversion_examples() -> None:
    # The documented translations (GitHub -> Quartz) jobs.yml is built from.
    assert to_quartz("15 */6 * * *") == "0 15 */6 * * ?"
    assert to_quartz("30 3 * * *") == "0 30 3 * * ?"
    assert to_quartz("30 5 * * *") == "0 30 5 * * ?"
    assert to_quartz("45 4 * * 1") == "0 45 4 ? * MON"  # GH 1 = Monday
    assert to_quartz("0 6 * * 1") == "0 0 6 ? * MON"
    assert to_quartz("0 6 * * 0") == "0 0 6 ? * SUN"  # GH 0 = Sunday


# --------------------------------------------------------------------------
# Cron parity: the Databricks DAG mirrors SourceSpec and GitHub exactly
# --------------------------------------------------------------------------


def test_every_job_is_scheduled_and_structurally_sound() -> None:
    jobs = JOBS["resources"]["jobs"]
    assert jobs, "no jobs in the bundle"

    required_env = {
        "TERRASENTINEL_ROOT",
        "DATABRICKS_SECRET_SCOPE",
        "HF_NAMESPACE",
        "DATABRICKS_DUCKDB_PATH",
        "UC_CATALOG",
        "UC_SCHEMA",
    }
    for key, job in jobs.items():
        assert job["name"].startswith("terrasentinel-"), key
        quartz = job["schedule"]["quartz_cron_expression"]
        parts = quartz.split()
        assert len(parts) == 6, f"{key}: Quartz cron needs seconds: {quartz!r}"
        assert parts[0] == "0", f"{key}: seconds must be 0: {quartz!r}"
        assert job["schedule"]["timezone_id"] == "Etc/UTC"

        cluster = job["job_clusters"][0]
        assert cluster["job_cluster_key"] == "worker"
        new_cluster = cluster["new_cluster"]
        assert new_cluster["num_workers"] == 0  # single-node: shared /tmp hands artifacts between tasks
        assert required_env <= set(new_cluster["spark_env_vars"]), key

        assert job["tasks"], key
        for task in job["tasks"]:
            assert task["job_cluster_key"] == "worker"
            script = task["spark_python_task"]["python_file"]
            assert (REPO / script).is_file(), f"{key}/{task['task_key']}: missing {script}"
            params = task["spark_python_task"]["parameters"]
            assert all(isinstance(p, str) for p in params)


def test_collect_jobs_mirror_source_registry() -> None:
    jobs = JOBS["resources"]["jobs"]
    for source_id, spec in SOURCES.items():
        job = jobs[f"collect_{source_id}"]
        assert job["schedule"]["quartz_cron_expression"] == to_quartz(spec.cadence_cron), source_id
        assert job_parameters(job, "collect") == ["--source", source_id]


def test_collect_data_workflow_crons_match_source_registry() -> None:
    crons = scheduled_crons(REPO / ".github/workflows/collect_data.yml")
    assert set(crons) == {spec.cadence_cron for spec in SOURCES.values()}


def test_transform_and_train_crons_match_github() -> None:
    jobs = JOBS["resources"]["jobs"]
    transform_cron = scheduled_crons(REPO / ".github/workflows/transform.yml")[0]
    train_cron = scheduled_crons(REPO / ".github/workflows/train_model.yml")[0]
    assert jobs["transform"]["schedule"]["quartz_cron_expression"] == to_quartz(transform_cron)
    assert jobs["train"]["schedule"]["quartz_cron_expression"] == to_quartz(train_cron)


def test_transform_task_parameters() -> None:
    jobs = JOBS["resources"]["jobs"]
    assert job_parameters(jobs["transform"], "run_dbt") == ["--target", "databricks"]
    assert job_parameters(jobs["transform"], "merge_gold") == ["--role", "transform"]
    train_params = job_parameters(jobs["train"], "train_model")
    assert "--mlflow-uri" in train_params
    assert train_params[train_params.index("--mlflow-uri") + 1] == "databricks"


# --------------------------------------------------------------------------
# Dependency pins: bundle libraries must come from the reviewed locks
# --------------------------------------------------------------------------


def _iter_pypi_packages(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pypi" and isinstance(value, dict) and "package" in value:
                found.append(str(value["package"]))
            else:
                found.extend(_iter_pypi_packages(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_pypi_packages(item))
    return found


def test_pypi_pins_come_from_lockfiles() -> None:
    lock_lines: set[str] = set()
    for lock in (REPO / "requirements").glob("*.lock"):
        for line in lock.read_text(encoding="utf-8").splitlines():
            lock_lines.add(line.rstrip(" \\").strip())

    packages = _iter_pypi_packages(JOBS)
    assert packages, "no pypi libraries declared on any task"
    for package in packages:
        name, sep, version = package.partition("==")
        assert sep and name and version, f"pin must be name==version, got {package!r}"
        assert package in lock_lines, f"{package} is not pinned in any requirements/*.lock"


def test_secret_scope_env_names_come_from_redact_registry() -> None:
    """Names our scripts export for libraries that read os.environ directly must be
    redactable (so a leaked log line cannot leak the credential itself)."""
    from databricks.jobs.batch_score import _PUBLISH_ENV

    allowed = set(SECRET_ENV_VARS) | {"TURSO_DATABASE_URL"}  # URL carries no token
    assert set(_PUBLISH_ENV) <= allowed


# --------------------------------------------------------------------------
# Generated UC DDL / MERGE
# --------------------------------------------------------------------------


def _create_columns(table: str) -> list[str]:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS \S+\.{table} \((.*?)\) COMMENT",
        SQL_TEXT,
        re.S,
    )
    assert match, f"no CREATE TABLE for {table}"
    return [line.strip().split()[0] for line in match.group(1).splitlines() if line.strip()]


def _merge_statement(table: str) -> str:
    match = re.search(rf"MERGE INTO \S+\.{table} AS target\n.*?;", SQL_TEXT, re.S)
    assert match, f"no MERGE for {table}"
    return match.group(0)


def test_generated_ddl_covers_every_gold_table() -> None:
    assert "-- GENERATED by `python -m tools.export_uc_ddl`" in SQL_TEXT
    assert "CREATE SCHEMA IF NOT EXISTS terrasentinel.gold;" in SQL_TEXT
    assert SQL_TEXT.count("CREATE TABLE IF NOT EXISTS") == len(GOLD_TABLES)
    assert SQL_TEXT.count("MERGE INTO") == len(GOLD_TABLES)

    for table, keys in GOLD_TABLES.items():
        columns = _create_columns(table)
        assert columns, table
        assert len(columns) == len(set(columns)), f"{table}: duplicate columns"

        merge = _merge_statement(table)
        assert f"USING stg_{table} AS source" in merge

        # Merge keys: exactly the GOLD_TABLES upsert keys, in declaration order.
        on_lines = [line for line in merge.splitlines() if line.startswith(("ON ", "AND "))]
        on_keys = [line.split()[1].rsplit(".", 1)[-1] for line in on_lines]
        assert on_keys == list(keys), f"{table}: ON keys {on_keys} != {list(keys)}"
        assert on_lines[0].startswith("ON "), f"{table}: first match line must be ON, got {on_lines[0]!r}"

        # UPDATE/INSERT cover writable columns only, and both agree.
        update_block = merge.split("WHEN MATCHED THEN UPDATE SET")[1].split("WHEN NOT MATCHED")[0]
        update_cols = [line.strip().rstrip(",").split("=")[0].strip() for line in update_block.splitlines() if "=" in line]
        insert_cols = [
            col.strip()
            for col in merge.split("THEN INSERT (")[1].split(")")[0].split(",")
        ]
        assert insert_cols == update_cols, f"{table}: INSERT/UPDATE column sets differ"
        assert not set(insert_cols) & set(ML_OWNED_COLUMNS), f"{table}: ML-owned columns must not be written"
        assert set(keys) <= set(insert_cols), f"{table}: keys must be writable"

        # The table must carry the ML-owned columns exactly when its dbt model
        # declares them (the scorer fills them; the transform never touches them).
        model = REPO / "transform/models/marts" / f"{table}.sql"
        assert model.is_file(), f"no dbt model for {table}"
        model_text = model.read_text(encoding="utf-8")
        for ml_col in ML_OWNED_COLUMNS:
            assert (ml_col in columns) == (f"as {ml_col}" in model_text), f"{table}: {ml_col} presence drifted"


def test_merge_gold_parses_the_generated_file() -> None:
    from databricks.jobs import merge_gold

    statements = merge_gold._split_statements(SQL_TEXT)
    creates = [s for s in statements if s.upper().startswith("CREATE")]
    assert len(creates) == len(GOLD_TABLES) + 1  # schema + tables

    merges: dict[str, str] = {}
    for statement in statements:
        if statement.upper().startswith("MERGE"):
            match = merge_gold._MERGE_TABLE.match(statement)
            assert match, statement[:80]
            merges[match.group(1)] = statement
    assert set(merges) == set(GOLD_TABLES)


def test_merge_gold_catalog_override_rewrites_schema_and_tables_together() -> None:
    from databricks.jobs import merge_gold

    rewritten = SQL_TEXT.replace(merge_gold._DEFAULT_PREFIX, "othercat.othersch")
    assert "terrasentinel.gold" not in rewritten
    assert "CREATE SCHEMA IF NOT EXISTS othercat.othersch;" in rewritten
    assert "MERGE INTO othercat.othersch.gold_fire_anomalies AS target" in rewritten
    assert "othercat.othersch.." not in rewritten
    # Table names themselves must survive the rewrite.
    assert "gold_fire_anomalies (" in rewritten


@pytest.mark.skipif(
    not (REPO / "transform/terrasentinel.duckdb").is_file(),
    reason="local dbt build not present (CI checks freshness after its synthetic build)",
)
def test_generated_ddl_is_fresh_against_the_local_build() -> None:
    from databricks.jobs import merge_gold
    from tools.export_uc_ddl import build_sql

    partial = build_sql(
        str(REPO / "transform/terrasentinel.duckdb"),
        catalog="terrasentinel",
        schema="gold",
        allow_missing=True,
    )
    for statement in merge_gold._split_statements(partial):
        assert statement in SQL_TEXT, (
            "databricks/sql/gold_ddl.sql is stale for the local lake — "
            "regenerate with `python -m tools.export_uc_ddl`"
        )


# --------------------------------------------------------------------------
# dbt profile target
# --------------------------------------------------------------------------


def test_databricks_dbt_target_differs_from_dev_only_in_path() -> None:
    outputs = yaml.safe_load((REPO / "transform/profiles.yml").read_text(encoding="utf-8"))["terrasentinel"][
        "outputs"
    ]
    dev, databricks = outputs["dev"], outputs["databricks"]

    assert databricks["type"] == "duckdb"
    assert "DATABRICKS_DUCKDB_PATH" in databricks["path"]

    # The default path is the bundle's variable defaults made concrete, and
    # the cluster env carries the same path built from the same variables.
    expected_default = (
        f"/Volumes/{BUNDLE['variables']['uc_catalog']['default']}"
        f"/{BUNDLE['variables']['uc_schema']['default']}/pipeline/terrasentinel.duckdb"
    )
    assert expected_default in databricks["path"]

    worker_env = JOBS["resources"]["jobs"]["collect_firms"]["job_clusters"][0]["new_cluster"]["spark_env_vars"]
    assert worker_env["DATABRICKS_DUCKDB_PATH"] == (
        "/Volumes/${var.uc_catalog}/${var.uc_schema}/pipeline/terrasentinel.duckdb"
    )

    dev_minus_path = {k: v for k, v in dev.items() if k != "path"}
    dbx_minus_path = {k: v for k, v in databricks.items() if k != "path"}
    assert dev_minus_path == dbx_minus_path


# --------------------------------------------------------------------------
# Entry scripts + dispatch registry
# --------------------------------------------------------------------------


def test_entry_scripts_are_self_contained_and_bootstrap_the_repo_root() -> None:
    scripts = sorted((REPO / "databricks/jobs").glob("*.py"))
    assert {p.name for p in scripts} == {
        "collect.py",
        "run_dbt.py",
        "merge_gold.py",
        "train.py",
        "batch_score.py",
    }
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        ast.parse(text)  # syntax gate (ruff catches style; this catches reality)
        # spark_python_task stages only this one file: it must locate the repo
        # itself rather than import sibling modules.
        assert "_repo_root" in text, path.name
        assert "TERRASENTINEL_ROOT" in text, path.name
        assert 'if __name__ == "__main__":' in text, path.name


def test_source_dispatch_covers_exactly_the_registry() -> None:
    from collectors.backfill_historical import COLLECTOR_CLASSES
    from databricks.jobs.collect import SOURCE_MODULES

    assert set(SOURCE_MODULES) == set(SOURCES)
    assert set(COLLECTOR_CLASSES) == set(SOURCES)
    for source_id, module in SOURCE_MODULES.items():
        assert (REPO / Path(*module.split(".")).with_suffix(".py")).is_file(), source_id


# --------------------------------------------------------------------------
# Secrets hygiene + git hygiene
# --------------------------------------------------------------------------


def test_bundle_declares_no_secret_valued_env_keys() -> None:
    # Credentials reach tasks via collectors.config.get_secret (env first,
    # secret scope second) — the YAML must not grow secret-shaped keys at all.
    jobs_text = (REPO / "databricks/resources/jobs.yml").read_text(encoding="utf-8")
    assert "{{secrets/" not in jobs_text
    for name in SECRET_ENV_VARS:
        assert f"{name}:" not in jobs_text, f"{name} must not appear as a YAML key"
    # The scope name (not a credential) is the only secret-related wiring.
    assert "DATABRICKS_SECRET_SCOPE" in jobs_text


def test_databricks_docs_folder_is_gitignored() -> None:
    entries = {line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "databricks/docs/" in entries


def test_repo_parses_under_python_311_grammar() -> None:
    """The default ``spark_version`` (DBR 15.4) ships Python 3.11.

    The repo develops on 3.12, so a single 3.12-only construct (``def f[T]``,
    ``class C[T]``, a PEP 701 f-string) would stay green on the 3.12 CI runner
    and SyntaxError at import time on every cluster task. Parsing the whole
    tree with the 3.11 grammar makes that impossible — and if you later point
    ``spark_version`` at a runtime with Python >= 3.12, this test is the place
    to record that the constraint was lifted.
    """
    infra = {".venv", "venv", "node_modules", "dist", "build", ".git", "mlruns", "mlartifacts", "serving"}
    failures: list[str] = []
    for path in sorted(REPO.rglob("*.py")):
        if infra & set(path.parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(REPO)}:{exc.lineno} {exc.msg}")
    assert not failures, "Python 3.12-only syntax (breaks DBR 15.4 tasks):\n" + "\n".join(failures)
