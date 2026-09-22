"""The ML layer: the artifact contract, tracking, evaluation and validation.

Most of these guard against *silent* failures — a bundle whose feature columns drifted
from what was fitted, a scoring run whose percentiles are not comparable to training, a
known-events check that quietly skips because a column is missing.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.bundle import ModelBundle, load_bundle, save_bundle, score_to_percentile
from ml.tracking import TrackedRun, tracked_run
from ml.train.evaluate import (
    agreement_with_reference,
    concentration,
    evaluate,
    percentile_rank,
    separation,
)
from ml.validation.known_events import (
    KNOWN_EVENTS,
    KnownEvent,
    check_known_events,
)


class FakeModel:
    """Stands in for IsolationForest: scores by distance from a fixed point."""

    def __init__(self, centre: float = 0.0) -> None:
        self.centre = centre

    def score_samples(self, matrix: pd.DataFrame) -> np.ndarray:
        return -np.abs(matrix.to_numpy(dtype=float).sum(axis=1) - self.centre)


def make_bundle(**overrides) -> ModelBundle:
    defaults = dict(
        model=FakeModel(),
        feature_columns=["a", "b"],
        anomaly_percentile_threshold=0.975,
        contamination=0.025,
        model_kind="test_model",
        trained_on={"dataset_commit": "abc123", "rows": 100},
        score_reference=[0.0, 0.1, 0.2, 0.3, 0.4],
    )
    defaults.update(overrides)
    return ModelBundle(**defaults)


class TestModelBundle:
    def test_refuses_an_empty_feature_list(self) -> None:
        with pytest.raises(ValueError, match="feature_columns"):
            make_bundle(feature_columns=[])

    @pytest.mark.parametrize("threshold", [0.0, -0.1, 1.5])
    def test_rejects_an_impossible_threshold(self, threshold: float) -> None:
        with pytest.raises(ValueError, match="anomaly_percentile_threshold"):
            make_bundle(anomaly_percentile_threshold=threshold)

    @pytest.mark.parametrize("contamination", [0.0, 0.5, 0.9])
    def test_rejects_an_impossible_contamination(self, contamination: float) -> None:
        with pytest.raises(ValueError, match="contamination"):
            make_bundle(contamination=contamination)

    def test_version_is_deterministic_and_input_derived(self) -> None:
        # Retraining on the same data with the same parameters must produce the same
        # version, or the predictions table accumulates duplicate rows per model.
        first, second = make_bundle(), make_bundle()
        assert first.version == second.version
        assert first.version != make_bundle(trained_on={"dataset_commit": "different"}).version

    def test_version_ignores_the_fitted_object(self) -> None:
        assert make_bundle(model=FakeModel(centre=9.0)).version == make_bundle().version

    def test_scored_label_includes_kind_and_version(self) -> None:
        bundle = make_bundle()
        assert bundle.scored_label() == f"test_model:{bundle.version}"

    def test_describe_is_json_serialisable_and_hides_the_model(self) -> None:
        payload = json.loads(json.dumps(make_bundle().describe(), default=str))
        assert "model" not in payload
        assert payload["model_class"] == "FakeModel"
        assert payload["dataset_commit"] == "abc123"

    def test_round_trips_through_disk(self, tmp_path: Path) -> None:
        path = save_bundle(make_bundle(), tmp_path)
        loaded = load_bundle(tmp_path)
        assert loaded.feature_columns == ["a", "b"]
        assert loaded.score_reference == [0.0, 0.1, 0.2, 0.3, 0.4]
        assert (tmp_path / "bundle_summary.json").is_file()
        assert path.is_file()

    def test_loading_a_directory_and_a_file_agree(self, tmp_path: Path) -> None:
        save_bundle(make_bundle(), tmp_path)
        assert load_bundle(tmp_path).version == load_bundle(tmp_path / "model_bundle.joblib").version

    def test_refuses_an_unrecognised_artifact(self, tmp_path: Path) -> None:
        import joblib

        joblib.dump({"not": "a bundle"}, tmp_path / "model_bundle.joblib")
        with pytest.raises(TypeError, match="not a ModelBundle"):
            load_bundle(tmp_path)

    def test_missing_artifact_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_bundle(tmp_path / "nope")


class TestScoreToPercentile:
    def test_ranks_against_the_training_distribution(self) -> None:
        bundle = make_bundle(score_reference=[0.0, 1.0, 2.0, 3.0])
        percentiles, comparable = score_to_percentile(bundle, np.array([-5.0, 3.0, 10.0]))
        assert comparable is True
        assert percentiles[0] == 0.25  # below everything
        assert percentiles[1] == 1.0   # at the maximum
        assert percentiles[2] == 1.0   # beyond the range saturates

    def test_without_a_reference_it_falls_back_and_says_so(self) -> None:
        # Silently producing non-comparable percentiles would be worse than an
        # explicit approximation.
        bundle = make_bundle(score_reference=[])
        percentiles, comparable = score_to_percentile(bundle, np.array([1.0, 2.0, 3.0]))
        assert comparable is False
        assert 0 < percentiles[-1] <= 1.0


class TestPercentileRank:
    def test_is_monotone_and_bounded(self) -> None:
        reference = np.arange(100, dtype=float)
        ranks = percentile_rank(reference, np.array([-10.0, 0.0, 50.0, 99.0, 1000.0]))
        assert np.all(np.diff(ranks) >= 0)
        assert ranks.min() > 0 and ranks.max() <= 1.0

    def test_rejects_an_empty_reference(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            percentile_rank(np.array([]), np.array([1.0]))


class TestEvaluation:
    def test_score_distribution_reports_quantiles(self) -> None:
        from ml.train.evaluate import score_distribution

        report = score_distribution(np.arange(1000, dtype=float))
        assert report["score_min"] == 0.0
        assert report["score_max"] == 999.0
        assert 0.5 <= report["score_p500"] / 999 <= 0.51

    def test_score_distribution_rejects_empty(self) -> None:
        from ml.train.evaluate import score_distribution

        with pytest.raises(ValueError, match="empty"):
            score_distribution(np.array([]))

    def test_agreement_detects_an_identical_model(self) -> None:
        # The measured case: a model that merely re-derives the reference rule. The
        # flags must use the same cutoff the function does, or the comparison is
        # between two different rules. This exercises the *comparator* with identical
        # inputs — it says nothing about what training passes in. The training path
        # is pinned separately, against the gold mart's own flag, in
        # tests/test_train_isolation_forest.py.
        scores = np.arange(100, dtype=float)
        flags = scores >= np.quantile(scores, 1.0 - 0.025)
        report = agreement_with_reference(scores, flags, top_fraction=0.025)
        assert report["precision_vs_reference"] == 1.0
        assert report["jaccard"] == 1.0

    def test_agreement_is_below_one_when_the_model_disagrees(self) -> None:
        # The regression this guards: passing the model's own flags as the reference
        # made every agreement metric a constant 1.0, which is unfalsifiable by
        # construction. A reference that flags the *quiet* end cannot coincide with
        # the model's top slice, so both metrics must come out strictly below one.
        scores = np.arange(100, dtype=float)
        flags = np.zeros(100, dtype=bool)
        flags[:10] = True  # the rule flags the ten lowest-scoring days
        report = agreement_with_reference(scores, flags, top_fraction=0.05)

        assert report["model_selected"] == 5.0
        assert report["reference_flagged"] == 10.0
        assert report["overlap"] == 0.0
        assert report["precision_vs_reference"] < 1.0
        assert report["jaccard"] < 1.0

    def test_agreement_is_partial_when_the_disagreement_is_partial(self) -> None:
        # Half of the model's flagged days are also flagged by the rule: precision
        # must reflect exactly that, rather than collapsing to either 1.0 or 0.0.
        scores = np.arange(100, dtype=float)
        flags = np.zeros(100, dtype=bool)
        flags[95:97] = True   # two of the model's top five
        flags[0:3] = True     # three days the model will not select
        report = agreement_with_reference(scores, flags, top_fraction=0.05)

        assert report["model_selected"] == 5.0
        assert report["reference_flagged"] == 5.0
        assert report["overlap"] == 2.0
        assert report["precision_vs_reference"] == 0.4
        assert report["jaccard"] < 1.0

    def test_agreement_detects_a_disjoint_model(self) -> None:
        scores = np.arange(100, dtype=float)
        flags = scores < 2.5  # reference flags the bottom
        report = agreement_with_reference(scores, flags, top_fraction=0.025)
        assert report["precision_vs_reference"] == 0.0
        assert report["overlap"] == 0.0

    def test_agreement_rejects_misaligned_inputs(self) -> None:
        with pytest.raises(ValueError, match="size mismatch"):
            agreement_with_reference(np.arange(5), np.array([True, False]), top_fraction=0.2)

    def test_separation_measures_the_flagged_slice(self) -> None:
        frame = pd.DataFrame({"v": [1.0] * 100 + [1000.0] * 3})
        scores = np.concatenate([np.zeros(100), np.ones(3)])
        report = separation(frame, scores, value_column="v", top_fraction=0.03)
        assert report["v_ratio"] > 100

    def test_concentration_is_low_for_an_informative_nothing_model(self) -> None:
        # A ramp is what "no discrimination" looks like: the top slice holds roughly
        # twice its share of mass and no more.
        report = concentration(np.arange(1000, dtype=float), top_fraction=0.025)
        assert report["score_mass_in_top_slice"] < 0.10

    def test_a_constant_score_is_reported_as_fully_concentrated(self) -> None:
        # Degenerate by construction: every point sits at the cutoff, so the "top
        # slice" is everything. Callers should read 1.0 here as "no discrimination".
        report = concentration(np.ones(1000), top_fraction=0.025)
        assert report["score_mass_in_top_slice"] == 1.0

    def test_concentration_is_high_when_mass_is_concentrated(self) -> None:
        scores = np.zeros(1000)
        scores[-5:] = 100.0
        report = concentration(scores, top_fraction=0.01)
        assert report["score_mass_in_top_slice"] > 0.9

    def test_report_flags_a_model_that_re_derives_the_rule(self) -> None:
        frame = pd.DataFrame({"detection_count": np.arange(100, dtype=float)})
        scores = np.arange(100, dtype=float)
        flags = scores >= np.quantile(scores, 1.0 - 0.025)
        report = evaluate(frame, scores, reference_flags=flags, top_fraction=0.025)
        assert any("re-deriving the baseline" in note for note in report.notes)

    def test_report_always_states_that_labels_are_absent(self) -> None:
        frame = pd.DataFrame({"detection_count": np.arange(10, dtype=float)})
        report = evaluate(frame, np.arange(10, dtype=float))
        assert any("never accuracy or F1" in note for note in report.notes)

    def test_report_says_what_the_reference_actually_is(self) -> None:
        # The metric is only interpretable if the reader knows the comparator is the
        # gold mart's flag and not the model's own output.
        frame = pd.DataFrame({"detection_count": np.arange(100, dtype=float)})
        scores = np.arange(100, dtype=float)
        flags = scores >= np.quantile(scores, 0.975)
        report = evaluate(frame, scores, reference_flags=flags, top_fraction=0.025)
        assert any("gold_fire_anomalies.is_anomaly" in note for note in report.notes)
        assert any("constant 1.0" in note for note in report.notes)


def scored_frame(**overrides) -> pd.DataFrame:
    base = pd.DataFrame(
        {
            "region_id": ["iberia_fire"] * 5,
            "observation_date": pd.to_datetime(
                ["2025-08-14", "2025-08-15", "2025-08-16", "2025-08-17", "2025-07-26"]
            ),
            "detection_count": [500, 13329, 9429, 8669, 172],
            "anomaly_percentile": [0.90, 0.999, 0.99, 0.98, 0.60],
            "is_anomaly": [False, True, True, True, False],
        }
    )
    for key, value in overrides.items():
        base[key] = value
    return base


class TestKnownEvents:
    def test_catches_a_documented_event_above_the_serving_threshold(self) -> None:
        outcomes = check_known_events(
            scored_frame(),
            events=(
                KnownEvent(
                    region_id="iberia_fire",
                    start=date(2025, 8, 15),
                    end=date(2025, 8, 17),
                    description="test",
                    evidence="test",
                    measured_peak_detections=13000,
                ),
            ),
        )
        assert outcomes[0].passed
        assert outcomes[0].criterion == "flag"

    def test_catches_an_event_by_rank_when_below_the_threshold(self) -> None:
        # The real case for Greece 2024-09-30: an extreme z-score that the lag-based
        # model ranks highly but does not flag.
        frame = pd.DataFrame(
            {
                "region_id": ["greece_fire"],
                "observation_date": pd.to_datetime(["2024-09-30"]),
                "detection_count": [808],
                "anomaly_percentile": [0.934],
                "is_anomaly": [False],
            }
        )
        outcomes = check_known_events(
            frame,
            events=(
                KnownEvent(
                    region_id="greece_fire",
                    start=date(2024, 9, 30),
                    end=date(2024, 9, 30),
                    description="test",
                    evidence="test",
                    measured_peak_detections=808,
                ),
            ),
        )
        assert outcomes[0].passed
        assert outcomes[0].criterion == "rank"

    def test_negative_control_passes_when_unflagged(self) -> None:
        outcomes = check_known_events(
            scored_frame(),
            events=(
                KnownEvent(
                    region_id="iberia_fire",
                    start=date(2025, 7, 26),
                    end=date(2025, 7, 26),
                    description="control",
                    evidence="test",
                    measured_peak_detections=172,
                    expect_detected=False,
                ),
            ),
        )
        assert outcomes[0].passed
        assert outcomes[0].criterion == "clean"

    def test_negative_control_fails_when_flagged(self) -> None:
        frame = scored_frame(is_anomaly=[False, True, True, True, True])
        outcomes = check_known_events(
            frame,
            events=(
                KnownEvent(
                    region_id="iberia_fire",
                    start=date(2025, 7, 26),
                    end=date(2025, 7, 26),
                    description="control",
                    evidence="test",
                    measured_peak_detections=172,
                    expect_detected=False,
                ),
            ),
        )
        assert not outcomes[0].passed
        assert "FALSE POSITIVE" in outcomes[0].verdict

    def test_a_vanished_window_is_reported_as_a_data_change(self) -> None:
        # Guards against silently passing when a backfill has changed the archive.
        frame = scored_frame(detection_count=[500, 10, 9, 8, 172])
        outcomes = check_known_events(
            frame,
            events=(
                KnownEvent(
                    region_id="iberia_fire",
                    start=date(2025, 8, 15),
                    end=date(2025, 8, 17),
                    description="test",
                    evidence="test",
                    measured_peak_detections=13329,
                ),
            ),
        )
        assert not outcomes[0].passed
        assert outcomes[0].criterion == "data-changed"

    def test_a_missing_window_is_reported_not_skipped(self) -> None:
        outcomes = check_known_events(
            scored_frame(),
            events=(
                KnownEvent(
                    region_id="iberia_fire",
                    start=date(2019, 1, 1),
                    end=date(2019, 1, 2),
                    description="test",
                    evidence="test",
                    measured_peak_detections=10,
                ),
            ),
        )
        assert not outcomes[0].passed
        assert "no data" in outcomes[0].verdict

    def test_missing_columns_raise_rather_than_skipping(self) -> None:
        with pytest.raises(KeyError, match="known-events check needs"):
            check_known_events(pd.DataFrame({"region_id": ["x"]}))

    def test_every_recorded_expectation_is_measured_not_invented(self) -> None:
        # The first draft contained an invented peak for 2025-07-26; expectations must
        # stay traceable to a measurement.
        for event in KNOWN_EVENTS:
            assert event.measured_peak_detections > 0, event.description
            assert event.evidence.strip(), event.description
            assert "Measured" in event.evidence or "NEGATIVE CONTROL" in event.description

    def test_at_least_one_negative_control_exists(self) -> None:
        assert any(not event.expect_detected for event in KNOWN_EVENTS)


class TestTracking:
    def test_requires_a_dataset_commit(self) -> None:
        with pytest.raises(ValueError, match="dataset_commit is required"):
            with tracked_run("run", dataset_commit=None):
                pass

    def test_json_fallback_records_the_run(self, tmp_path: Path) -> None:
        with tracked_run(
            "unit_test_run", dataset_commit="abc123", prefer_mlflow=False, local_dir=tmp_path
        ) as run:
            run.log_params({"n_estimators": 300})
            run.log_metrics({"score": 1.5})
            assert run.backend == "json"

        written = list(tmp_path.glob("*.json"))
        assert len(written) == 1
        payload = json.loads(written[0].read_text())
        assert payload["params"]["n_estimators"] == "300"
        assert payload["metrics"]["score"] == 1.5
        assert payload["tags"]["dataset_commit"] == "abc123"
        assert payload["status"] == "FINISHED"

    def test_json_fallback_records_failure(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError):
            with tracked_run(
                "failing_run", dataset_commit="abc123", prefer_mlflow=False, local_dir=tmp_path
            ):
                raise RuntimeError("boom")

        payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
        assert payload["status"] == "FAILED"
        assert "boom" in payload["error"]

    def test_log_params_skips_empty_values(self, tmp_path: Path) -> None:
        with tracked_run(
            "run", dataset_commit="abc", prefer_mlflow=False, local_dir=tmp_path
        ) as run:
            run.log_params({"set": "yes", "unset": None})

        payload = json.loads(json.dumps(run.params))
        assert "unset" not in payload

    def test_tracked_run_is_serialisable(self) -> None:
        run = TrackedRun(name="x", backend="json", run_id="y")
        json.dumps(run.as_dict())
