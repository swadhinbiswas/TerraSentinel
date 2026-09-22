"""Sync the DuckDB gold marts into Turso.

dbt has no production-mature libSQL/Turso adapter, so this is a deliberate, explicit
pipeline step rather than a `target` — which makes a sync failure visible and
re-runnable on its own instead of being buried inside a transform run. See
docs/adr/0001-duckdb-over-spark.md and 0002-turso-over-postgres.md.

Three properties this script guarantees:

* **Idempotent.** Every write is an upsert on a declared primary key, because
  GitHub Actions jobs get re-run and partially fail, and a duplicated gold row
  silently corrupts every anomaly score computed from it.
* **Schema-managed.** Tables are created if absent and missing columns are added,
  so a new dbt column does not require a manual migration. Types are mapped to
  SQLite affinity; timestamps become ISO-8601 UTC text (which sorts correctly).
* **Counted and reported.** `--record-metadata` writes a pipeline_runs row so the
  dashboard's "last updated" indicator reflects the sync, not just the transform.

Usage::

    python -m sync.push_gold_to_turso --duckdb-path transform/terrasentinel.duckdb
    python -m sync.push_gold_to_turso --dry-run          # read and shape, write nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from ops.redact import redact_text
from ops.resilience import PermanentError
from storage.turso_client import TursoClient

LOGGER = logging.getLogger("terrasentinel.sync")

WORKFLOW = "transform"

#: Gold tables to publish to the serving database, with their upsert keys.
#:
#: These keys must identify exactly one row per mart — an upsert keyed on anything
#: narrower silently overwrites a distinct row. dbt asserts the same fact from the other
#: side, with `unique_combination_of_columns` in ``transform/models/marts/_marts.yml``,
#: so the two definitions have to stay compatible: **the key here may be a superset of
#: dbt's, never a subset.** (A superset of a unique key is still unique; a subset is
#: not.) The one superset today is `gold_h3_fire`, keyed by three columns here against
#: dbt's two, because its `metric_type` is the constant `'fire_detection_count'`.
#: `tests/test_sync_turso.py` parses the dbt yml and enforces the relationship, because
#: nothing else would notice the two drifting apart: dbt only sees the mart, and the sync
#: only sees its own key.
GOLD_TABLES: dict[str, list[str]] = {
    # Each mart depends on exactly one source, so a source outage removes only its own
    # table rather than blanking a combined one.
    "gold_fire_anomalies": ["region_id", "observation_date"],
    "gold_ice_extent_trends": ["region_id", "period_start"],
    "gold_h3_fire": ["h3_index", "period_start", "metric_type"],
    "gold_h3_sst": ["h3_index", "period_start", "metric_type"],
    # Sentinel-dependent tables; absent until the GEE backfill runs.
    "gold_deforestation_index": ["region_id", "month_start"],
    "gold_glacier_backscatter": ["region_id", "period_start"],
    "gold_h3_sentinel": ["h3_index", "period_start", "metric_type"],
}

#: Columns owned by the ML layer rather than by dbt. Excluded from the insert so a
#: sync can never blank out a score the batch scoring job has already written.
ML_OWNED_COLUMNS: frozenset[str] = frozenset({"anomaly_score", "model_version", "scored_at"})


def sqlite_type(duckdb_type: str) -> str:
    """Map a DuckDB type to SQLite storage affinity."""
    declared = duckdb_type.upper()
    if "TIMESTAMP" in declared or "DATE" in declared:
        # Stored as ISO-8601 UTC text: SQLite has no date type and this sorts correctly.
        return "TEXT"
    if "BOOL" in declared:
        return "INTEGER"
    if any(token in declared for token in ("DOUBLE", "FLOAT", "REAL", "DECIMAL")):
        return "REAL"
    if any(token in declared for token in ("INT", "LONG", "SHORT")):
        return "INTEGER"
    if "BLOB" in declared:
        return "BLOB"
    return "TEXT"


@dataclass(slots=True)
class TablePlan:
    name: str
    key_columns: list[str]
    columns: list[tuple[str, str]]  # (name, sqlite type)
    create_sql: str
    #: Original DuckDB types, kept so `date` and `timestamp` can be serialised
    #: differently — both map to TEXT, and conflating them makes a date column
    #: unmatchable by anything that stores plain dates.
    duckdb_types: dict[str, str] = field(default_factory=dict)
    missing_columns: list[tuple[str, str]] = field(default_factory=list)

    @property
    def writable_columns(self) -> list[str]:
        return [name for name, _ in self.columns if name not in ML_OWNED_COLUMNS]


def describe_table(connection: duckdb.DuckDBPyConnection, schema: str, table: str) -> list[tuple[str, str]]:
    rows = connection.execute(
        """
        select column_name, data_type
        from information_schema.columns
        where table_schema = ? and table_name = ?
        order by ordinal_position
        """,
        [schema, table],
    ).fetchall()
    if not rows:
        raise PermanentError(
            f"{schema}.{table} has no columns — did the transform run? "
            f"(looked in {connection.execute('select current_database()').fetchone()[0]})"
        )
    return [(str(name), str(dtype)) for name, dtype in rows]


def table_exists(
    connection: duckdb.DuckDBPyConnection, schema: str, table: str
) -> bool:
    """Whether a mart has been built at all.

    Absence is a legitimate state, not a failure: marts whose source has not been
    backfilled do not exist yet, and failing the whole sync for them would block the
    tables that are ready — the same coupling bug that the dbt split removed.
    """
    row = connection.execute(
        """
        select 1 from information_schema.tables
        where table_schema = ? and table_name = ?
        """,
        [schema, table],
    ).fetchone()
    return row is not None


def plan_table(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    key_columns: Sequence[str],
    *,
    schema: str = "gold",
) -> TablePlan:
    described = describe_table(connection, schema, table)
    names = [name for name, _ in described]
    missing_keys = [key for key in key_columns if key not in names]
    if missing_keys:
        raise PermanentError(
            f"{table} is missing its declared primary key column(s) {missing_keys}; "
            f"available: {names}"
        )

    columns = [(name, sqlite_type(dtype)) for name, dtype in described]
    column_sql = ",\n    ".join(f"{name} {dtype}" for name, dtype in columns)
    create_sql = (
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        f"    {column_sql},\n"
        f"    PRIMARY KEY ({', '.join(key_columns)})\n"
        f")"
    )
    return TablePlan(
        name=table,
        key_columns=list(key_columns),
        columns=columns,
        create_sql=create_sql,
        duckdb_types=dict(described),
    )


def coerce_value(value: Any, *, duckdb_type: str | None = None) -> Any:
    """Convert a pandas/DuckDB value into something the libSQL client can encode.

    The serialisation traps here are specific: a ``NaN`` left in an object column
    becomes the literal string ``"nan"``, a numpy scalar is not JSON-encodable even
    though it looks like a number, and a DATE must serialise as ``YYYY-MM-DD`` rather
    than as a full timestamp — otherwise a join against anything storing plain dates
    silently never matches.
    """
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        if duckdb_type and "TIMESTAMP" not in duckdb_type.upper() and "DATE" in duckdb_type.upper():
            return pd.Timestamp(value).date().isoformat()
        return pd.Timestamp(value).isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return coerce_value(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def frame_to_rows(
    frame: pd.DataFrame,
    columns: Iterable[str],
    duckdb_types: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    selected = list(columns)
    missing = [column for column in selected if column not in frame.columns]
    if missing:
        raise PermanentError(f"frame is missing column(s) {missing}; has {list(frame.columns)}")
    types = duckdb_types or {}
    return [
        {column: coerce_value(record[column], duckdb_type=types.get(column)) for column in selected}
        for record in frame[selected].to_dict(orient="records")
    ]


def read_gold(connection: duckdb.DuckDBPyConnection, table: str, *, schema: str = "gold") -> pd.DataFrame:
    return connection.execute(f"select * from {schema}.{table}").df()


def reconcile_columns(client: TursoClient, plan: TablePlan) -> list[str]:
    """Create the table if needed and add any columns dbt has gained since.

    SQLite cannot change a column's type, but adding one is cheap and means a new
    mart column does not require a manual migration before the next deploy.
    """
    client.execute(plan.create_sql)
    existing = {
        str(row["name"])
        for row in client.fetch_all(f"PRAGMA table_info({plan.name})")
        if row.get("name")
    }
    added: list[str] = []
    for name, dtype in plan.columns:
        if name in existing:
            continue
        client.execute(f"ALTER TABLE {plan.name} ADD COLUMN {name} {dtype}")
        added.append(name)
    return added


def sync_table(
    client: TursoClient,
    connection: duckdb.DuckDBPyConnection,
    table: str,
    key_columns: Sequence[str],
    *,
    dry_run: bool = False,
    batch_rows: int | None = None,
    recreate: bool = False,
) -> dict[str, Any]:
    plan = plan_table(connection, table, key_columns)
    frame = read_gold(connection, table)

    if recreate and not dry_run:
        # Escape hatch for a change to how a *primary key* column serialises. An
        # upsert cannot replace a row whose key value changed — it inserts a second
        # one — so a date/timestamp format fix orphans every existing row. Dropping
        # and rebuilding is the only correct repair, and it has to be explicit
        # because it discards whatever is in the serving table.
        LOGGER.warning("dropping %s for a clean rebuild (--recreate)", table)
        client.execute(f"DROP TABLE IF EXISTS {table}")  # type: ignore[union-attr]

    if frame.empty:
        # A mart with zero rows is not a sync failure, but it is worth saying out
        # loud because it usually means an upstream stage produced nothing.
        LOGGER.warning("%s is empty; nothing to sync", table)
        return {"table": table, "rows": 0, "rows_written": 0, "columns_added": [], "empty": True}

    rows = frame_to_rows(frame, plan.writable_columns, plan.duckdb_types)
    result: dict[str, Any] = {
        "table": table,
        "rows": len(rows),
        "rows_written": 0,
        "key_columns": plan.key_columns,
        "columns_added": [],
        "empty": False,
    }
    if dry_run:
        result["dry_run"] = True
        # Prove the payload is encodable without writing it.
        result["null_columns"] = sorted(
            column for column in plan.writable_columns if all(row[column] is None for row in rows)
        )
        return result

    result["columns_added"] = reconcile_columns(client, plan)
    result["rows_written"] = client.upsert(
        table, rows, key_columns=plan.key_columns, chunk_rows=batch_rows
    )
    return result


def sync_all(
    *,
    duckdb_path: str | Path,
    tables: Mapping[str, Sequence[str]] | None = None,
    client: TursoClient | None = None,
    dry_run: bool = False,
    batch_rows: int | None = None,
    recreate: bool = False,
) -> dict[str, Any]:
    selected = dict(tables or GOLD_TABLES)
    path = Path(duckdb_path)
    if not path.is_file():
        raise PermanentError(
            f"DuckDB database {path} not found — run `dbt build` before syncing"
        )

    connection = duckdb.connect(str(path), read_only=True)
    results: list[dict[str, Any]] = []
    try:
        if not dry_run:
            client = client or TursoClient()
        for table, keys in selected.items():
            if not table_exists(connection, "gold", table):
                # Reported, never silently skipped: an operator should be able to see
                # which tables are waiting on a source.
                LOGGER.warning(
                    "%s has not been built (its source is not backfilled yet); skipping",
                    table,
                )
                results.append(
                    {
                        "table": table,
                        "status": "absent",
                        "rows": 0,
                        "rows_written": 0,
                        "columns_added": [],
                        "empty": True,
                    }
                )
                continue
            results.append(
                sync_table(
                    client,  # type: ignore[arg-type]
                    connection,
                    table,
                    keys,
                    dry_run=dry_run,
                    batch_rows=batch_rows,
                    recreate=recreate,
                )
            )
    finally:
        connection.close()

    present = [item for item in results if item.get("status") != "absent"]
    return {
        "duckdb_path": str(path),
        "dry_run": dry_run,
        "tables": results,
        "tables_synced": len(present),
        "tables_absent": len(results) - len(present),
        "total_rows": sum(item["rows"] for item in results),
        "total_written": sum(item["rows_written"] for item in results),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--duckdb-path",
        default=os.environ.get("DUCKDB_PATH", "transform/terrasentinel.duckdb"),
        help="DuckDB file produced by dbt (default: %(default)s)",
    )
    parser.add_argument(
        "--tables",
        default="all",
        help="comma list of gold tables, or 'all' (default: all)",
    )
    parser.add_argument("--batch-rows", type=int, default=None, help="rows per upsert statement")
    parser.add_argument("--dry-run", action="store_true", help="shape the payload, write nothing")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help=(
            "drop and rebuild each table instead of upserting. Required after changing how "
            "a primary-key column serialises, because an upsert cannot replace a row whose "
            "key changed — it adds a duplicate. Discards existing rows in those tables."
        ),
    )
    parser.add_argument("--no-metadata", action="store_true", help="skip the pipeline_runs row")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.tables.strip().lower() in {"", "all"}:
        tables = dict(GOLD_TABLES)
    else:
        chosen = [value.strip() for value in args.tables.split(",") if value.strip()]
        unknown = [value for value in chosen if value not in GOLD_TABLES]
        if unknown:
            raise SystemExit(f"unknown table(s) {unknown}. Known: {sorted(GOLD_TABLES)}")
        tables = {name: GOLD_TABLES[name] for name in chosen}

    try:
        report = sync_all(
            duckdb_path=args.duckdb_path,
            tables=tables,
            dry_run=args.dry_run,
            batch_rows=args.batch_rows,
            recreate=args.recreate,
        )
    except PermanentError as exc:
        LOGGER.error("sync failed: %s", redact_text(exc))
        _record(args, status="failed", rows=0, error=str(exc))
        return 2

    print(json.dumps(report, indent=2, default=str))
    _record(args, status="success", rows=report["total_written"], details=report)
    return 0


def _record(
    args: argparse.Namespace,
    *,
    status: str,
    rows: int,
    error: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    if args.no_metadata or args.dry_run:
        return
    try:
        from collectors.config import get_secret
        from ops.pipeline_run import PipelineRunRecorder
        from storage.hf_dataset_writer import HFDatasetWriter

        turso = None
        if get_secret("TURSO_DATABASE_URL", required=False) and get_secret(
            "TURSO_AUTH_TOKEN", required=False
        ):
            turso = TursoClient()
        hf_writer = None
        if get_secret("HF_TOKEN", required=False):
            hf_writer = HFDatasetWriter(repo_kind="bronze")

        PipelineRunRecorder(workflow=WORKFLOW, hf_writer=hf_writer, turso=turso).record(
            status=status, rows_written=rows, error=error, details=details or {}
        )
    except Exception as exc:  # noqa: BLE001 - metadata must not break the sync
        LOGGER.error("could not record run metadata: %s", redact_text(exc))


if __name__ == "__main__":
    raise SystemExit(main())
