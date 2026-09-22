"""Gold-mart sync: type mapping, value coercion, and idempotent upserts.

The failure modes this guards against are the quiet ones: a `NaN` serialised as the
string "nan", numpy scalars that are not JSON-encodable, and a blind insert that
double-counts rows on a re-run.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pytest

from ops.resilience import PermanentError
from sync.push_gold_to_turso import (
    GOLD_TABLES,
    ML_OWNED_COLUMNS,
    coerce_value,
    frame_to_rows,
    main,
    plan_table,
    reconcile_columns,
    sqlite_type,
    sync_all,
    sync_table,
)


class RecordingTurso:
    """Stands in for TursoClient, capturing DDL and upserts."""

    def __init__(self, existing: dict[str, list[str]] | None = None) -> None:
        self.existing = existing or {}
        self.ddl: list[str] = []
        self.upserts: list[dict[str, Any]] = []

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any]:
        self.ddl.append(sql)
        return {"rows": [], "affected": 0}

    def fetch_all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if sql.upper().startswith("PRAGMA TABLE_INFO"):
            table = sql.split("(")[1].rstrip(")")
            if table not in self.existing:
                return []
            return [{"name": name} for name in self.existing[table]]
        return []

    def upsert(
        self,
        table: str,
        rows: Any,
        *,
        key_columns: Any,
        chunk_rows: int | None = None,
    ) -> int:
        materialised = [dict(row) for row in rows]
        self.upserts.append(
            {"table": table, "rows": materialised, "keys": list(key_columns)}
        )
        return len(materialised)


def gold_db(*, empty: bool = False) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute("create schema gold")
    connection.execute(
        """
        create table gold.gold_fire_anomalies as
        select * from (values
            ('iberia_fire', date '2026-08-15', 56, 16.86, true, 'extreme', cast(null as double), cast(null as varchar)),
            ('greece_fire', date '2026-08-15', 4, -0.34, false, 'normal', cast(null as double), cast(null as varchar))
        ) as t(region_id, observation_date, detection_count, zscore, is_anomaly, severity,
               anomaly_score, model_version)
        where not ?
        """,
        [empty],
    )
    connection.execute("alter table gold.gold_fire_anomalies add column updated_at timestamp with time zone")
    connection.execute("update gold.gold_fire_anomalies set updated_at = now()")
    return connection


class TestTypeMapping:
    @pytest.mark.parametrize(
        "duckdb_type,expected",
        [
            ("DOUBLE", "REAL"),
            ("BIGINT", "INTEGER"),
            ("INTEGER", "INTEGER"),
            ("VARCHAR", "TEXT"),
            ("BOOLEAN", "INTEGER"),
            ("TIMESTAMP WITH TIME ZONE", "TEXT"),
            ("DATE", "TEXT"),
            ("BLOB", "BLOB"),
            ("HUGEINT", "INTEGER"),
        ],
    )
    def test_sqlite_affinity(self, duckdb_type: str, expected: str) -> None:
        assert sqlite_type(duckdb_type) == expected


class TestPlanTable:
    def test_generates_create_with_primary_key(self) -> None:
        plan = plan_table(gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"])
        assert "CREATE TABLE IF NOT EXISTS gold_fire_anomalies" in plan.create_sql
        assert "PRIMARY KEY (region_id, observation_date)" in plan.create_sql
        assert ("zscore", "REAL") in plan.columns
        assert ("observation_date", "TEXT") in plan.columns

    def test_missing_key_column_is_rejected(self) -> None:
        with pytest.raises(PermanentError, match="primary key"):
            plan_table(gold_db(), "gold_fire_anomalies", ["nonexistent_key"])

    def test_unknown_table_is_rejected(self) -> None:
        with pytest.raises(PermanentError, match="no columns"):
            plan_table(gold_db(), "gold_nope", ["a"])

    def test_ml_owned_columns_are_excluded_from_writes(self) -> None:
        plan = plan_table(gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"])
        for column in ML_OWNED_COLUMNS:
            assert column not in plan.writable_columns
        assert "zscore" in plan.writable_columns


class TestCoerceValue:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            (float("nan"), None),
            (float("inf"), None),
            (True, True),
            (7, 7),
            ("text", "text"),
            (Decimal("1.5"), 1.5),
            (pd.NaT, None),
        ],
    )
    def test_scalars(self, value: Any, expected: Any) -> None:
        assert coerce_value(value) == expected

    def test_timestamps_become_iso_utc_text(self) -> None:
        assert coerce_value(pd.Timestamp("2026-08-15T00:00:00", tz="UTC")).startswith(
            "2026-08-15T00:00:00"
        )

    def test_dates_become_iso_text(self) -> None:
        assert coerce_value(date(2026, 8, 15)) == "2026-08-15T00:00:00"

    def test_numpy_scalars_are_unwrapped(self) -> None:
        import numpy as np

        assert coerce_value(np.int64(42)) == 42
        assert coerce_value(np.float64(1.5)) == 1.5
        assert coerce_value(np.float64("nan")) is None

    def test_pandas_na_becomes_none(self) -> None:
        assert coerce_value(pd.NA) is None


class TestFrameToRows:
    def test_nan_never_becomes_the_string_nan(self) -> None:
        frame = pd.DataFrame({"a": [1.0, float("nan")], "b": ["x", "y"]})
        rows = frame_to_rows(frame, ["a", "b"])
        assert rows[1]["a"] is None

    def test_missing_column_is_rejected(self) -> None:
        with pytest.raises(PermanentError, match="missing column"):
            frame_to_rows(pd.DataFrame({"a": [1]}), ["a", "b"])

    def test_payload_is_json_encodable(self) -> None:
        import numpy as np

        frame = pd.DataFrame(
            {
                "ts": pd.to_datetime(["2026-08-15"], utc=True),
                "n": np.array([3], dtype="int64"),
                "f": np.array([1.25], dtype="float64"),
            }
        )
        rows = frame_to_rows(frame, ["ts", "n", "f"])
        json.dumps(rows)  # the libSQL client serialises to JSON


class TestReconcileColumns:
    def test_creates_the_table_and_adds_missing_columns(self) -> None:
        plan = plan_table(gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"])
        client = RecordingTurso(existing={"gold_fire_anomalies": ["region_id", "observation_date"]})

        added = reconcile_columns(client, plan)

        assert any(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql in client.ddl)
        assert "detection_count" in added
        assert "anomaly_score" in added
        assert any("ALTER TABLE gold_fire_anomalies ADD COLUMN zscore REAL" in sql for sql in client.ddl)

    def test_no_alter_when_every_column_exists(self) -> None:
        plan = plan_table(gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"])
        client = RecordingTurso(
            existing={"gold_fire_anomalies": [name for name, _ in plan.columns]}
        )
        assert reconcile_columns(client, plan) == []
        assert not any("ALTER TABLE" in sql for sql in client.ddl)


class TestSyncTable:
    def test_writes_with_declared_keys(self) -> None:
        client = RecordingTurso()
        report = sync_table(
            client, gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"]
        )

        assert report["rows"] == 2
        assert report["rows_written"] == 2
        assert client.upserts[0]["keys"] == ["region_id", "observation_date"]
        assert client.upserts[0]["rows"][0]["region_id"] == "iberia_fire"

    def test_values_are_serialisable(self) -> None:
        client = RecordingTurso()
        sync_table(client, gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"])
        json.dumps(client.upserts[0]["rows"])

    def test_ml_scores_are_not_overwritten(self) -> None:
        # A sync must never blank a score the scoring job already wrote.
        client = RecordingTurso()
        sync_table(client, gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"])
        assert not set(client.upserts[0]["rows"][0]) & ML_OWNED_COLUMNS

    def test_empty_mart_is_reported_not_failed(self, caplog) -> None:
        client = RecordingTurso()
        report = sync_table(
            client, gold_db(empty=True), "gold_fire_anomalies", ["region_id", "observation_date"]
        )
        assert report["empty"] is True
        assert report["rows_written"] == 0
        assert client.upserts == []

    def test_dry_run_writes_nothing_but_shapes_the_payload(self) -> None:
        client = RecordingTurso()
        report = sync_table(
            client,
            gold_db(),
            "gold_fire_anomalies",
            ["region_id", "observation_date"],
            dry_run=True,
        )
        assert report["rows"] == 2
        assert report["rows_written"] == 0
        assert client.upserts == []
        assert client.ddl == []
        # ML-owned columns are excluded from the payload entirely, so they never
        # appear here — the sync cannot blank a score the scoring job wrote.
        assert "anomaly_score" not in report["null_columns"]
        assert "updated_at" not in report["null_columns"]


class TestSyncAll:
    def test_syncs_every_configured_table(self, tmp_path) -> None:
        pytest.importorskip("duckdb")
        path = tmp_path / "warehouse.duckdb"
        connection = duckdb.connect(str(path))
        connection.execute("create schema gold")
        connection.execute(
            "create table gold.gold_fire_anomalies as select 'iberia_fire' as region_id, "
            "date '2026-08-15' as observation_date, 5 as detection_count"
        )
        connection.close()

        client = RecordingTurso()
        report = sync_all(
            duckdb_path=path,
            tables={"gold_fire_anomalies": ["region_id", "observation_date"]},
            client=client,
        )

        assert report["total_rows"] == 1
        assert report["total_written"] == 1
        assert report["tables"][0]["table"] == "gold_fire_anomalies"

    def test_missing_database_fails_with_guidance(self, tmp_path) -> None:
        with pytest.raises(PermanentError, match="dbt build"):
            sync_all(duckdb_path=tmp_path / "nope.duckdb", client=RecordingTurso())

    def test_default_table_set_matches_the_gold_marts(self) -> None:
        # One mart per source arm, so an outage removes only its own table.
        assert set(GOLD_TABLES) == {
            "gold_fire_anomalies",
            "gold_ice_extent_trends",
            "gold_deforestation_index",
            "gold_glacier_backscatter",
            "gold_h3_fire",
            "gold_h3_sst",
            "gold_h3_sentinel",
        }


class TestCli:
    def test_dry_run_prints_a_report(self, tmp_path, capsys) -> None:
        path = tmp_path / "warehouse.duckdb"
        connection = duckdb.connect(str(path))
        connection.execute("create schema gold")
        connection.execute(
            "create table gold.gold_fire_anomalies as select 'iberia_fire' as region_id, "
            "date '2026-08-15' as observation_date, 5 as detection_count"
        )
        connection.close()

        code = main(
            [
                "--duckdb-path",
                str(path),
                "--tables",
                "gold_fire_anomalies",
                "--dry-run",
            ]
        )
        report = json.loads(capsys.readouterr().out)
        assert code == 0
        assert report["dry_run"] is True
        assert report["total_rows"] == 1

    def test_unknown_table_is_rejected(self, tmp_path) -> None:
        with pytest.raises(SystemExit, match="unknown table"):
            main(["--duckdb-path", str(tmp_path / "x.duckdb"), "--tables", "gold_nope"])


class TestAbsentMarts:
    """A mart whose source has not been backfilled must not block the others.

    The same coupling bug the dbt split removed: one missing source should never take
    down tables that are ready to serve.
    """

    def test_absent_mart_is_skipped_and_reported(self, tmp_path) -> None:
        path = tmp_path / "warehouse.duckdb"
        connection = duckdb.connect(str(path))
        connection.execute("create schema gold")
        connection.execute(
            "create table gold.gold_fire_anomalies as "
            "select 'iberia_fire' as region_id, date '2026-08-15' as observation_date, 5 as detection_count"
        )
        connection.close()

        client = RecordingTurso()
        report = sync_all(
            duckdb_path=path,
            tables={
                "gold_fire_anomalies": ["region_id", "observation_date"],
                "gold_h3_sentinel": ["h3_index", "period_start", "metric_type"],
            },
            client=client,
        )

        by_table = {item["table"]: item for item in report["tables"]}
        assert by_table["gold_fire_anomalies"]["rows_written"] == 1
        assert by_table["gold_h3_sentinel"]["status"] == "absent"
        assert report["tables_synced"] == 1
        assert report["tables_absent"] == 1
        # Only the ready table was written.
        assert [entry["table"] for entry in client.upserts] == ["gold_fire_anomalies"]

    def test_table_exists_detects_presence(self) -> None:
        from sync.push_gold_to_turso import table_exists

        connection = gold_db()
        assert table_exists(connection, "gold", "gold_fire_anomalies") is True
        assert table_exists(connection, "gold", "gold_nope") is False


class TestDateSerialisation:
    """A DATE column must serialise as a date, not a timestamp.

    Both map to SQLite TEXT, so the difference is invisible until a join against a
    table that stores plain dates silently matches nothing — which is exactly what
    happened between the gold marts and ml_predictions.
    """

    def test_date_column_becomes_iso_date(self) -> None:
        value = pd.Timestamp("2026-02-25 00:00:00")
        assert coerce_value(value, duckdb_type="DATE") == "2026-02-25"

    def test_timestamp_column_keeps_full_precision(self) -> None:
        value = pd.Timestamp("2026-02-25 13:45:00")
        assert coerce_value(value, duckdb_type="TIMESTAMP WITH TIME ZONE").startswith("2026-02-25T13:45")

    def test_unknown_type_defaults_to_timestamp(self) -> None:
        value = pd.Timestamp("2026-02-25 13:45:00")
        assert "T13:45" in coerce_value(value)

    def test_plan_records_duckdb_types(self) -> None:
        plan = plan_table(gold_db(), "gold_fire_anomalies", ["region_id", "observation_date"])
        assert "DATE" in plan.duckdb_types["observation_date"].upper()

    def test_frame_to_rows_applies_per_column_types(self) -> None:
        frame = pd.DataFrame({"d": pd.to_datetime(["2026-02-25"]), "ts": pd.to_datetime(["2026-02-25 06:00"], utc=True)})
        rows = frame_to_rows(frame, ["d", "ts"], {"d": "DATE", "ts": "TIMESTAMP WITH TIME ZONE"})
        assert rows[0]["d"] == "2026-02-25"
        assert "T06:00" in rows[0]["ts"]


class TestDbtKeyConsistency:
    """`GOLD_TABLES` and dbt's uniqueness tests must describe the same row identity.

    The sync upserts on `GOLD_TABLES`; dbt asserts uniqueness with
    `unique_combination_of_columns`. Nothing else compares them, and neither side would
    fail alone if they drifted: dbt only sees the mart, and the sync only sees its own
    key. The required relationship is containment — the sync key may be a *superset* of
    dbt's (gold_h3_fire adds a constant `metric_type`) but never a subset, because a
    subset lets two distinct rows share an upsert key and silently overwrite each other.
    """

    MARTS = Path(__file__).resolve().parents[1] / "transform" / "models" / "marts" / "_marts.yml"

    @staticmethod
    def dbt_keys() -> dict[str, list[str]]:
        """Map each mart to the columns its dbt uniqueness test asserts."""
        yaml = pytest.importorskip("yaml")
        document = yaml.safe_load(TestDbtKeyConsistency.MARTS.read_text(encoding="utf-8"))
        keys: dict[str, list[str]] = {}
        for model in document["models"]:
            for test in model.get("tests") or []:
                if isinstance(test, dict) and "unique_combination_of_columns" in test:
                    spec = test["unique_combination_of_columns"]
                    arguments = spec.get("arguments", spec) if isinstance(spec, dict) else spec
                    keys[model["name"]] = list(arguments["combination_of_columns"])
        return keys

    def test_every_gold_table_is_covered_by_a_dbt_uniqueness_test(self) -> None:
        dbt = self.dbt_keys()
        assert set(dbt) == set(GOLD_TABLES), (
            f"only in dbt: {sorted(set(dbt) - set(GOLD_TABLES))}; "
            f"only in GOLD_TABLES: {sorted(set(GOLD_TABLES) - set(dbt))}"
        )

    def test_sync_keys_are_supersets_of_the_dbt_keys(self) -> None:
        for table, sync_key in GOLD_TABLES.items():
            dbt_key = self.dbt_keys()[table]
            assert set(dbt_key) <= set(sync_key), (
                f"{table}: sync keys {sync_key} do not cover dbt's {dbt_key} — "
                "two rows dbt considers distinct could share an upsert key"
            )
            assert len(sync_key) == len(set(sync_key)), f"{table}: repeated key column in {sync_key}"
