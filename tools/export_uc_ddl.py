"""Generate the Unity Catalog DDL + MERGE for the gold marts.

Reads column names and types from the DuckDB file that dbt actually built (so
the SQL can never describe a schema the models do not produce) and combines
them with the upsert keys in ``GOLD_TABLES`` and the ML-owned columns in
``ML_OWNED_COLUMNS`` (which the transform merge must never write).

Output: ``databricks/sql/gold_ddl.sql`` — executed idempotently by
``databricks/jobs/merge_gold.py`` and reviewable in PRs. Regenerate after any
change to a mart model::

    python -m tools.export_uc_ddl            # rewrite the file
    python -m tools.export_uc_ddl --check    # CI: fail if the file is stale

``--check`` is the same freshness gate as ``tools.export_seeds``.

Contract encoded in the generated SQL:

* ``CREATE SCHEMA`` / ``CREATE TABLE IF NOT EXISTS`` — idempotent bootstrap.
* ``MERGE`` keys are exactly ``GOLD_TABLES[table]`` (a superset of dbt's
  unique keys, never a subset — same rule as the Turso push).
* ``UPDATE SET`` / ``INSERT`` cover writable columns only: ``anomaly_score``,
  ``model_version`` and ``scored_at`` are owned by the ML layer and are never
  blanked by a transform-side write.

Absent tables (a source that has not been backfilled yet) are an error by
default — a committed file silently missing half the gold tables would shrink
the Delta side. ``--allow-missing`` generates a partial file for local
experimentation; CI never uses it.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from sync.push_gold_to_turso import GOLD_TABLES, ML_OWNED_COLUMNS

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "databricks" / "sql" / "gold_ddl.sql"
DEFAULT_DUCKDB = os.environ.get("DUCKDB_PATH", str(REPO_ROOT / "transform" / "terrasentinel.duckdb"))

#: Canonical types for the ML-owned columns, used in comments only — the
#: columns themselves are read from DuckDB like every other column.
_ML_COMMENT_ORDER = ("anomaly_score", "model_version", "scored_at")


def spark_type(duckdb_type: str) -> str:
    """Map a DuckDB column type to its Spark/Delta equivalent.

    Conservative by construction: widening (HUGEINT -> DECIMAL(38,0)) is always
    safe for a MERGE, narrowing is not. Unrecognised types collapse to STRING
    rather than failing the build — rows arrive as parquet written by DuckDB,
    so Spark re-derives the physical type and casts on assignment; the declared
    type only has to be *compatible*.
    """
    declared = " ".join(str(duckdb_type).upper().split())
    if declared.startswith(("DECIMAL(", "NUMERIC(")):
        return declared.replace("NUMERIC", "DECIMAL", 1)
    if "TIMESTAMP" in declared:
        # Delta TIMESTAMP is UTC-normalised; duckdb TIMESTAMPTZ stores UTC too.
        return "TIMESTAMP"
    if declared == "DATE":
        return "DATE"
    if "BOOL" in declared:
        return "BOOLEAN"
    if "DOUBLE" in declared:
        return "DOUBLE"
    if "REAL" in declared or "FLOAT" in declared:
        return "FLOAT"
    # Integer family: order matters — HUGEINT contains INT, INTEGER starts INT.
    if declared.startswith(("HUGEINT", "UHUGEINT")):
        return "DECIMAL(38,0)"  # int128 has no Delta type
    if declared.startswith(("BIGINT", "UBIGINT")):
        return "BIGINT"
    if declared.startswith(("INTEGER", "UINTEGER")) or declared == "INT":
        return "INT"
    if declared.startswith(("SMALLINT", "USMALLINT")):
        return "SHORT"
    if declared.startswith(("TINYINT", "UTINYINT")):
        return "BYTE"
    if "INT" in declared:
        return "BIGINT"
    if "BLOB" in declared or "BYTEA" in declared:
        return "BINARY"
    if declared.startswith(("VARCHAR", "CHAR")) or declared in {"TEXT", "STRING", "JSON", "UUID"}:
        return "STRING"
    if declared.startswith(("TIME", "INTERVAL")):  # no Delta TIME/INTERVAL analogue we rely on
        return "STRING"
    return "STRING"


def _schema_and_missing(connection: duckdb.DuckDBPyConnection, tables: Sequence[str]) -> tuple[str, list[str]]:
    """Locate the single DuckDB schema holding the gold marts; list absent tables."""
    placeholders = ", ".join("?" for _ in tables)
    rows = connection.execute(
        f"""
        select distinct table_schema, table_name
        from information_schema.tables
        where table_name in ({placeholders})
        """,
        list(tables),
    ).fetchall()
    if not rows:
        raise SystemExit("no gold tables found — did the transform build them?")
    schemas = {str(schema) for schema, _ in rows}
    if len(schemas) > 1:
        raise SystemExit(f"gold tables split across schemas {sorted(schemas)}; refusing to guess")
    present = {str(name) for _, name in rows}
    missing = [table for table in tables if table not in present]
    return schemas.pop(), missing


def _columns_of(
    connection: duckdb.DuckDBPyConnection, duckdb_schema: str, table: str
) -> list[tuple[str, str]]:
    return [
        (str(name), str(dtype))
        for name, dtype in connection.execute(
            """
            select column_name, data_type
            from information_schema.columns
            where table_schema = ? and table_name = ?
            order by ordinal_position
            """,
            [duckdb_schema, table],
        ).fetchall()
    ]


def render(
    entries: Sequence[tuple[str, list[str], list[tuple[str, str]]]],
    *,
    catalog: str,
    schema: str,
    duckdb_schema: str,
) -> str:
    """Render the full SQL file: (table, keys, duckdb columns) per entry."""
    qualified = f"{catalog}.{schema}"
    lines: list[str] = [
        "-- GENERATED by `python -m tools.export_uc_ddl` — DO NOT EDIT BY HAND.",
        "--",
        "-- Sources of truth:",
        # Deliberately no filename here: CI checks this file against its own
        # synthetic build (terrasentinel.duckdb), so only environment-stable
        # facts may appear in the header.
        f"--   * column names/types : the dbt-built DuckDB schema (duckdb schema `{duckdb_schema}`)",
        "--   * MERGE keys          : sync/push_gold_to_turso.py::GOLD_TABLES",
        "--   * never-updated cols  : sync/push_gold_to_turso.py::ML_OWNED_COLUMNS",
        "--",
        "-- Consumed by databricks/jobs/merge_gold.py: idempotent CREATEs, one",
        "-- key-matched MERGE per table (staging view name: stg_<table>).",
        "-- CI runs `python -m tools.export_uc_ddl --check` for staleness.",
        "",
        f"CREATE SCHEMA IF NOT EXISTS {qualified};",
        "",
    ]

    for table, keys, columns in entries:
        declared = dict(columns)
        writable = [name for name, _ in columns if name not in ML_OWNED_COLUMNS]

        column_lines = [f"    {name} {spark_type(dtype)}" for name, dtype in columns]
        comment_bits = [f"upsert keys: {', '.join(keys)}"]
        ml_here = [name for name in _ML_COMMENT_ORDER if name in declared]
        if ml_here:
            comment_bits.append(f"ML-owned (untouched by this merge): {', '.join(ml_here)}")

        lines += [
            f"CREATE TABLE IF NOT EXISTS {qualified}.{table} (",
            ",\n".join(column_lines),
            f") COMMENT '{table} | {' | '.join(comment_bits)}';",
            "",
            f"MERGE INTO {qualified}.{table} AS target",
            f"USING stg_{table} AS source",
        ]
        for index, key in enumerate(keys):
            # Multi-key match: one ON, subsequent keys AND-ed (separate ON
            # clauses are a syntax error in Delta MERGE).
            lines.append(f"{'ON' if index == 0 else 'AND'} target.{key} = source.{key}")

        # Keys are also writable columns; UPDATE/INSERT cover the full writable
        # set (setting a key to the matched value is a no-op by construction of
        # the ON clause, and keeps INSERT/UPDATE symmetric).
        sets = ",\n    ".join(f"{name} = source.{name}" for name in writable)
        lines.append("WHEN MATCHED THEN UPDATE SET\n    " + sets)
        insert_cols = ", ".join(writable)
        insert_vals = ", ".join(f"source.{name}" for name in writable)
        lines.append(f"WHEN NOT MATCHED THEN INSERT ({insert_cols})\n    VALUES ({insert_vals});")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_sql(duckdb_path: str, *, catalog: str, schema: str, allow_missing: bool) -> str:
    path = Path(duckdb_path)
    if not path.is_file():
        raise SystemExit(f"duckdb file not found: {path} (build the marts first)")

    connection = duckdb.connect(str(path), read_only=True)
    try:
        duckdb_schema, missing = _schema_and_missing(connection, list(GOLD_TABLES))
        if missing:
            hint = (
                " — run the backfill for the missing source(s), or pass "
                "--allow-missing for a partial file"
            )
            message = f"gold tables absent from {path.name}: {', '.join(missing)}{hint}"
            if not allow_missing:
                raise SystemExit(message)
            print(f"warning: {message}", file=sys.stderr)

        entries: list[tuple[str, list[str], list[tuple[str, str]]]] = []
        for table, keys in GOLD_TABLES.items():
            if table in missing:
                continue
            columns = _columns_of(connection, duckdb_schema, table)
            known = [name for name, _ in columns]
            absent_keys = [key for key in keys if key not in known]
            if absent_keys:
                raise SystemExit(f"{table} is missing its declared key column(s) {absent_keys}")
            entries.append((table, list(keys), columns))
    finally:
        connection.close()

    return render(entries, catalog=catalog, schema=schema, duckdb_schema=duckdb_schema)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duckdb", default=DEFAULT_DUCKDB, help="dbt-built DuckDB file to read the schema from")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="SQL file to write/check")
    parser.add_argument("--catalog", default=os.environ.get("UC_CATALOG", "terrasentinel"))
    parser.add_argument("--schema-name", dest="schema_name", default=os.environ.get("UC_SCHEMA", "gold"))
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale (CI gate)")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="generate a partial file when some gold tables are absent (local experiments only)",
    )
    args = parser.parse_args(argv)

    rendered = build_sql(
        args.duckdb,
        catalog=args.catalog,
        schema=args.schema_name,
        allow_missing=args.allow_missing,
    )
    out_path = Path(args.out)

    if args.check:
        current = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
        if current == rendered:
            print(f"{out_path} is up to date ({rendered.count('MERGE INTO')} MERGE statements)")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(),
            rendered.splitlines(),
            fromfile=str(out_path),
            tofile="regenerated",
            lineterm="",
        )
        print(f"stale: {out_path} — regenerate with `python -m tools.export_uc_ddl`", file=sys.stderr)
        for line in list(diff)[:60]:
            print(line, file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {out_path}: {rendered.count('CREATE TABLE')} tables, {rendered.count('MERGE INTO')} MERGEs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
