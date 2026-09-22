"""What `train()` must do that is easy to get silently wrong.

The bug this file exists to prevent: training evaluated the model against *its own*
flags, so `precision_vs_reference` and `jaccard` were 1.0 by construction and the
published claim built on them could never fail. The reference has to be
`gold_fire_anomalies.is_anomaly` — an independent statistical rule — and these tests
build a lake where the two genuinely disagree and assert the metrics say so.

Everything here runs offline against a hand-built DuckDB file.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.features.build_feature_table import FIRE_FEATURE_COLUMNS
from ml.registry.push_model_to_hf import render_model_card
from ml.train.train_isolation_forest import train

REGIONS = ("iberia_fire", "greece_fire")
DAYS = 200
#: Reference days are chosen from this offset onward, well past the lag windows, so
#: every flagged row survives `feature_matrix`'s dropna and the expected count is exact.
REFERENCE_MIN_OFFSET = 60
#: Quiet-but-ordinary days the *rule* flags — deliberately the days least likely to
#: land in an unsupervised model's top slice, so the two must disagree.
REFERENCE_DAYS_PER_REGION = 6


def make_gold_db(path: Path) -> pd.DataFrame:
    """A small but structurally real gold mart, plus the NOAA staging view.

    The reference flag is set on days near each region's *median* detection count —
    ordinary days the model should not flag — so any comparison against the model's own
    output can be told apart from a comparison against this one.
    """
    duckdb = pytest.importorskip("duckdb")
    rng = np.random.default_rng(7)

    records: list[dict[str, object]] = []
    for region in REGIONS:
        base = 45 if region == "iberia_fire" else 30
        for offset in range(DAYS):
            day = date(2020, 1, 1) + timedelta(days=offset)
            count = int(rng.poisson(base))
            if offset % 13 == 5:
                count = 0  # a genuinely empty day
            if offset % 37 == 11:
                count = 4200 + int(rng.integers(0, 500))  # a gross spike
            records.append(
                {
                    "region_id": region,
                    "observation_date": day,
                    "offset": offset,
                    "detection_count": count,
                    "h3_cell_count": max(1, count // 40),
                    "high_confidence_count": count // 2,
                    "frp_sum": float(count * 12),
                    "frp_max": 30.0 if count else 0.0,
                    "frp_mean": 12.0 if count else 0.0,
                    "confidence_pct_mean": 0.6 if count else None,
                }
            )

    frame = pd.DataFrame.from_records(records)
    # A robust-ish z so the column means what the gold mart's column means.
    def robust_z(values: pd.Series) -> pd.Series:
        centred = values - values.median()
        scale = 1.4826 * centred.abs().median()
        return centred / (scale or 1.0)

    frame["zscore"] = frame.groupby("region_id")["detection_count"].transform(robust_z)

    frame["is_anomaly"] = False
    for region in REGIONS:
        eligible = frame.loc[
            (frame["region_id"] == region) & (frame["offset"] >= REFERENCE_MIN_OFFSET),
            "detection_count",
        ]
        median = eligible.median()
        chosen = eligible.sub(median).abs().nsmallest(REFERENCE_DAYS_PER_REGION).index
        frame.loc[chosen, "is_anomaly"] = True
    frame = frame.drop(columns=["offset"])

    connection = duckdb.connect(str(path))
    try:
        connection.execute("create schema gold")
        connection.execute("create schema staging")
        connection.execute(
            """
            create table gold.gold_fire_anomalies as
            select region_id, observation_date, detection_count, h3_cell_count,
                   high_confidence_count, frp_sum, frp_max, frp_mean,
                   confidence_pct_mean, zscore, is_anomaly
            from frame
            """
        )
        connection.execute(
            """
            create table staging.stg_noaa as
            select region_id, observation_date::timestamp as observed_at,
                   'sst_anomaly'::varchar as metric_type, 0.4 as value
            from (select distinct region_id, observation_date from frame) using sample 40 rows
            """
        )
    finally:
        connection.close()
    return frame


@pytest.fixture
def gold_db(tmp_path: Path) -> Path:
    path = tmp_path / "terrasentinel.duckdb"
    make_gold_db(path)
    return path


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside tmp so mlflow's sqlite file and ml/runs land there, not in the repo."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    return tmp_path


class TestEvaluatesAgainstTheGoldReference:
    def test_agreement_metrics_can_disagree_with_the_model(
        self, gold_db: Path, workdir: Path
    ) -> None:
        pytest.importorskip("sklearn")
        pytest.importorskip("pyarrow")

        out = workdir / "artifacts"
        report = train(
            duckdb_path=gold_db,
            out_dir=out,
            dataset_commit="unit-test-commit",
            contamination=0.05,
            n_estimators=60,
        )
        metrics = report["metrics"]

        # The regression: these were both exactly 1.0 whenever the reference passed in
        # was the model's own flag vector.
        assert metrics["precision_vs_reference"] < 1.0
        assert metrics["jaccard"] < 1.0
        assert metrics["model_selected"] > 0
        assert metrics["reference_flagged"] > 0

    def test_the_reference_column_is_the_gold_mart_flag(
        self, gold_db: Path, workdir: Path
    ) -> None:
        pytest.importorskip("sklearn")
        pytest.importorskip("pyarrow")
        duckdb = pytest.importorskip("duckdb")

        out = workdir / "artifacts"
        train(
            duckdb_path=gold_db,
            out_dir=out,
            dataset_commit="unit-test-commit",
            contamination=0.05,
            n_estimators=60,
        )

        scored = pd.read_parquet(out / "scored_training_rows.parquet")
        assert {"is_anomaly", "reference_is_anomaly"} <= set(scored.columns)

        connection = duckdb.connect(str(gold_db), read_only=True)
        try:
            gold = connection.execute(
                "select region_id, observation_date, is_anomaly from gold.gold_fire_anomalies"
            ).df()
        finally:
            connection.close()
        gold["observation_date"] = pd.to_datetime(gold["observation_date"])
        scored["observation_date"] = pd.to_datetime(scored["observation_date"])

        merged = scored.merge(
            gold, on=["region_id", "observation_date"], suffixes=("", "_gold"), how="left"
        )
        assert merged["is_anomaly_gold"].notna().all(), "a scored row is missing from gold"
        # The reference carried into training *is* the mart's flag, row for row...
        assert (
            merged["reference_is_anomaly"].astype(bool)
            == merged["is_anomaly_gold"].astype(bool)
        ).all()
        # ...and the model's own flags are a different column that genuinely differs.
        diverges = merged["is_anomaly"].astype(bool) != merged["reference_is_anomaly"].astype(bool)
        assert diverges.sum() > 0

    def test_the_reference_count_in_the_report_matches_the_lake(
        self, gold_db: Path, workdir: Path
    ) -> None:
        pytest.importorskip("sklearn")
        duckdb = pytest.importorskip("duckdb")

        out = workdir / "artifacts"
        report = train(
            duckdb_path=gold_db,
            out_dir=out,
            dataset_commit="unit-test-commit",
            contamination=0.05,
            n_estimators=60,
        )

        connection = duckdb.connect(str(gold_db), read_only=True)
        try:
            gold = connection.execute(
                """
                select region_id, observation_date, is_anomaly
                from gold.gold_fire_anomalies
                where is_anomaly
                """
            ).df()
        finally:
            connection.close()
        gold["observation_date"] = pd.to_datetime(gold["observation_date"])

        scored = pd.read_parquet(out / "scored_training_rows.parquet")
        scored["observation_date"] = pd.to_datetime(scored["observation_date"])
        surviving = gold.merge(
            scored[["region_id", "observation_date"]],
            on=["region_id", "observation_date"],
            how="inner",
        )
        assert report["metrics"]["reference_flagged"] == float(len(surviving))

    def test_a_missing_reference_column_refuses_to_train(
        self, gold_db: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Refusing is the point: silently falling back to the model's own flags is how
        # the metric became a constant 1.0 in the first place.
        pytest.importorskip("sklearn")
        duckdb = pytest.importorskip("duckdb")
        from ml.features.build_feature_table import build_fire_features
        from ml.train import train_isolation_forest as module

        connection = duckdb.connect(str(gold_db), read_only=True)
        try:
            frame = build_fire_features(connection)
        finally:
            connection.close()

        monkeypatch.setattr(module, "build_fire_features", lambda _conn: frame.drop(columns=["is_anomaly"]))

        with pytest.raises(KeyError, match="is_anomaly is missing"):
            train(
                duckdb_path=gold_db,
                out_dir=workdir / "artifacts",
                dataset_commit="unit-test-commit",
                n_estimators=10,
            )

    def test_the_report_records_lineage_and_event_outcomes(
        self, gold_db: Path, workdir: Path
    ) -> None:
        import json

        pytest.importorskip("sklearn")
        out = workdir / "artifacts"
        report = train(
            duckdb_path=gold_db,
            out_dir=out,
            dataset_commit="unit-test-commit",
            contamination=0.05,
            n_estimators=60,
        )

        assert report["dataset_commit"] == "unit-test-commit"
        # The CI lake is synthetic and dated 2020, so the documented events cannot be
        # found — but the check must have *run* and said so, not been skipped.
        assert report["known_events"], "known-events check did not run"
        assert any(event["criterion"] == "no-data" for event in report["known_events"])

        from ml.bundle import load_bundle

        bundle = load_bundle(out)
        assert bundle.evaluation["known_events"]
        assert bundle.evaluation["reference_flagged"] == report["metrics"]["reference_flagged"]

        payload = json.loads((out / "training_report.json").read_text(encoding="utf-8"))
        assert payload["metrics"]["precision_vs_reference"] < 1.0
        assert payload["trained_on"]["reference_rule"].endswith("(is_anomaly, z >= 5)")


class TestModelCardReportsWhatWasMeasured:
    """The card is generated, so these pin what the generated text may claim."""

    @staticmethod
    def bundle_with(**evaluation: object):
        from ml.bundle import ModelBundle

        defaults: dict[str, object] = {
            "model_selected": 37.0,
            "reference_flagged": 12.0,
            "overlap": 5.0,
            "precision_vs_reference": 0.5,
            "reference_recall": 0.4,
            "jaccard": 0.3,
            "detection_count_ratio": 8.0,
            "detection_count_flagged_mean": 900.0,
            "detection_count_rest_mean": 60.0,
            "score_mass_in_top_slice": 0.5,
            "known_event_pass_rate": 1.0,
            "known_event_caught": 6.0,
            "known_event_total": 6.0,
            "known_events": [
                {
                    "region_id": "iberia_fire",
                    "window": "2025-08-15..2025-08-17",
                    "max_detections": 13329,
                    "criterion": "flag",
                    "passed": True,
                    "verdict": "detected by the serving threshold",
                },
                {
                    "region_id": "greece_fire",
                    "window": "2024-09-30..2024-09-30",
                    "max_detections": 808,
                    "criterion": "rank",
                    "passed": True,
                    "verdict": "detected by rank",
                },
            ],
        }
        defaults.update(evaluation)
        return ModelBundle(
            model=object(),
            feature_columns=list(FIRE_FEATURE_COLUMNS),
            anomaly_percentile_threshold=0.975,
            contamination=0.025,
            model_kind="isolation_forest_fire",
            trained_on={
                "dataset_commit": "abc123",
                "gold_table": "gold.gold_fire_anomalies",
                "rows": 1460,
                "regions": list(REGIONS),
                "first_day": "2024-09-21",
                "last_day": "2026-09-20",
                "reference_rule": "median/MAD z-score in gold.gold_fire_anomalies (is_anomaly, z >= 5)",
            },
            score_reference=[0.0, 1.0],
            evaluation=dict(defaults),
        )

    def test_partial_agreement_is_reported_without_a_limitation_claim(self) -> None:
        card = render_model_card(self.bundle_with())
        assert "precision 0.5000" in card
        assert "Jaccard 0.3000" in card
        assert "gold_fire_anomalies.is_anomaly" in card  # the reference is named
        # The "adds no information" claim is only allowed when agreement is total.
        assert "adds **no information**" not in card

    def test_total_agreement_is_still_reported_as_a_measured_limitation(self) -> None:
        card = render_model_card(
            self.bundle_with(
                reference_flagged=37.0,
                overlap=37.0,
                precision_vs_reference=1.0,
                reference_recall=1.0,
                jaccard=1.0,
            )
        )
        assert "Measured limitation" in card
        assert "37 of 37 days" in card
        assert "measured against" in card

    def test_the_event_list_comes_from_the_run_not_from_prose(self) -> None:
        card = render_model_card(self.bundle_with())
        assert "1 of 2 were caught only by rank" in card
        assert "caught by the serving threshold" in card
        # No hardcoded tally left in the generated card.
        assert "(6/6:" not in card

    def test_a_failed_event_check_is_not_written_as_a_pass(self) -> None:
        card = render_model_card(
            self.bundle_with(
                known_event_pass_rate=0.5,
                known_event_caught=1.0,
                known_event_total=2.0,
                known_events=[
                    {
                        "region_id": "iberia_fire",
                        "window": "2025-08-15..2025-08-17",
                        "max_detections": 13329,
                        "criterion": "missed",
                        "passed": False,
                        "verdict": "MISSED: peak 13329 detections",
                    }
                ],
            )
        )
        assert "**FAILED**" in card
        assert "MISSED" in card
        assert "(1/2 documented events)" in card

    def test_the_card_states_the_measured_row_count(self) -> None:
        card = render_model_card(self.bundle_with())
        assert "1,460 training rows" in card
        assert "~1,450" not in card
