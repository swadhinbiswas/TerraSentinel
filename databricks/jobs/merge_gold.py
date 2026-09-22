"""Databricks Workflow entry: publish DuckDB gold marts into Unity Catalog.

Executes the generated DDL/MERGE (``databricks/sql/gold_ddl.sql``, produced by
``python -m tools.export_uc_ddl`` from ``GOLD_TABLES`` — regenerate, never
hand-edit):

* ``CREATE SCHEMA`` / ``CREATE TABLE IF NOT EXISTS`` — idempotent bootstrap;
* one ``MERGE`` per table, keyed on the same upsert keys the Turso push uses,
  updating/inserting only the writable columns — the ML-owned columns
  (``anomaly_score``, ``model_version``, ``scored_at``) are never touched by
  this path, exactly like ``sync.push_gold_to_turso``.

Staging: each gold table is copied out of the DuckDB file as parquet (typed by
DuckDB itself, so no pandas type inference in the middle) into a Spark temp
view ``stg_<table>`` that the MERGE consumes. A table absent from the DuckDB
file — a source not yet backfilled — is skipped, mirroring the Turso push.

Self-contained like the other entry scripts: ``spark_python_task`` stages only
this file, so the repo root resolves from ``--repo-dir`` / ``TERRASENTINEL_ROOT``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

#: The ``catalog.schema`` prefix the generated SQL was written with; rewritten
#: at runtime when --catalog/--schema point elsewhere. No trailing dot on
#: purpose: ``CREATE SCHEMA IF NOT EXISTS terrasentinel.gold;`` has none, and
#: both the schema statement and the ``...gold.<table>`` references must move
#: together.
_DEFAULT_PREFIX = "terrasentinel.gold"

_MERGE_TABLE = re.compile(r"MERGE\s+INTO\s+\S+(?:\.\S+)?\.(\w+)\s+AS\s+", re.IGNORECASE)


def _repo_root(explicit: str | None) -> Path:
    """Locate the bundle-synced repo root (the driver has only this file)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_root = (os.environ.get("TERRASENTINEL_ROOT") or "").strip()
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parents[2])
    for candidate in candidates:
        for path in (candidate, Path("/Workspace") / str(candidate).lstrip("/")):
            if (path / "collectors" / "config.py").is_file():
                return path
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise SystemExit(f"repo root not found (tried: {tried}); pass --repo-dir or set TERRASENTINEL_ROOT")


def _split_statements(sql_text: str) -> list[str]:
    """Split on ';' and drop comment-only lines (the generator emits no ';' inside a statement)."""
    statements: list[str] = []
    for chunk in sql_text.split(";"):
        lines = [
            line
            for line in chunk.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        if lines:
            statements.append("\n".join(lines))
    return statements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=None, help="bundle-synced repo root")
    parser.add_argument("--role", choices=["transform"], default="transform")
    parser.add_argument("--duckdb-path", default=None, help="default: $DATABRICKS_DUCKDB_PATH")
    parser.add_argument("--catalog", default=os.environ.get("UC_CATALOG", "terrasentinel"))
    parser.add_argument("--schema", default=os.environ.get("UC_SCHEMA", "gold"))
    parser.add_argument("--sql", default="databricks/sql/gold_ddl.sql", help="path to the generated DDL/MERGE")
    parser.add_argument("--staging-dir", default="/tmp/terrasentinel/merge")
    parser.add_argument("--dry-run", action="store_true", help="print statements, write nothing to Unity Catalog")
    args = parser.parse_args(argv)

    root = _repo_root(args.repo_dir)
    sys.path.insert(0, str(root))

    duckdb_path = args.duckdb_path or os.environ.get("DATABRICKS_DUCKDB_PATH")
    if not duckdb_path:
        raise SystemExit("--duckdb-path or $DATABRICKS_DUCKDB_PATH is required")

    sql_path = Path(args.sql)
    if not sql_path.is_absolute():
        sql_path = root / sql_path
    sql_text = sql_path.read_text(encoding="utf-8")

    want_prefix = f"{args.catalog}.{args.schema}"
    if want_prefix != _DEFAULT_PREFIX:
        sql_text = sql_text.replace(_DEFAULT_PREFIX, want_prefix)

    statements = _split_statements(sql_text)
    creates = [statement for statement in statements if statement.upper().startswith("CREATE")]
    merges: dict[str, str] = {}
    for statement in statements:
        if statement.upper().startswith("MERGE"):
            match = _MERGE_TABLE.match(statement)
            if not match:
                raise SystemExit(f"cannot parse MERGE target from: {statement[:120]}")
            merges[match.group(1)] = statement
    if not creates or not merges:
        raise SystemExit(f"{sql_path} yielded {len(creates)} CREATE / {len(merges)} MERGE statements")

    import duckdb

    from sync.push_gold_to_turso import GOLD_TABLES, ML_OWNED_COLUMNS

    connection = duckdb.connect(duckdb_path)
    try:
        staging_root = Path(args.staging_dir)
        staging_root.mkdir(parents=True, exist_ok=True)

        spark = None
        if not args.dry_run:
            from pyspark.sql import SparkSession

            spark = SparkSession.builder.appName("terrasentinel-merge-gold").getOrCreate()
            for statement in creates:
                spark.sql(statement)
            print(f"[merge_gold] executed {len(creates)} DDL statement(s)")

        merged = skipped = 0
        for table, merge_sql in merges.items():
            if table not in GOLD_TABLES:
                print(f"[merge_gold] {table}: in SQL but not GOLD_TABLES — skipped", file=sys.stderr)
                continue
            exists = connection.execute(
                "SELECT 1 FROM information_schema.tables WHERE lower(table_name) = lower(?)",
                [table],
            ).fetchone()
            if not exists:
                print(f"[merge_gold] {table}: absent from the DuckDB file (source not built) — skipped")
                skipped += 1
                continue
            columns = [
                row[0]
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE lower(table_name) = lower(?) ORDER BY ordinal_position",
                    [table],
                ).fetchall()
            ]
            writable = [column for column in columns if column not in ML_OWNED_COLUMNS]
            if not writable:
                raise SystemExit(f"{table}: no writable columns discovered")
            column_list = ", ".join(f'"{column}"' for column in writable)
            parquet_path = staging_root / f"stg_{table}.parquet"
            escaped = str(parquet_path).replace("'", "''")
            connection.execute(f'COPY (SELECT {column_list} FROM "{table}") TO \'{escaped}\' (FORMAT PARQUET)')
            row_count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

            if args.dry_run:
                print(f"[merge_gold] {table}: would merge {row_count} row(s) via {parquet_path.name}")
                print(merge_sql)
                merged += 1
                continue
            assert spark is not None
            spark.read.parquet(str(parquet_path)).createOrReplaceTempView(f"stg_{table}")
            spark.sql(merge_sql)
            print(f"[merge_gold] {table}: merged {row_count} row(s)")
            merged += 1
    finally:
        connection.close()

    print(f"[merge_gold] done: {merged} merged, {skipped} skipped -> {want_prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
