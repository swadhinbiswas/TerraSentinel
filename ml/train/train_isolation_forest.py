"""Train the fire-risk Isolation Forest.

Why Isolation Forest for this: it is the right shape for the problem rather than the
easiest call. The data is tabular, the anomalies are *rare and different* rather than
merely extreme, and there are no labels. Isolation Forest works by asking how few random
splits it takes to isolate a point — genuinely unusual points isolate quickly — which is
exactly the question "is this day unlike the region's other days". One-Class SVM would
need tuning of a kernel on a small, heavy-tailed dataset; a distance-based method would
be dominated by the scale of fire counts.

Two design choices worth stating:

* **No feature scaling.** Isolation Forest splits on one feature at a time at random
  thresholds, so monotone rescaling cannot change the result. A scaler in the pipeline
  would add an artifact and imply a sensitivity that does not exist.
* **The score is a percentile.** See ``ml/bundle.py``.

Every run logs the dataset commit hash, so a model can always be traced to the exact
lake revision that produced it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from collectors.config import hf_repo
from ml.bundle import ModelBundle, save_bundle
from ml.features.build_feature_table import (
    FIRE_FEATURE_COLUMNS,
    build_fire_features,
    feature_matrix,
)
from ml.tracking import tracked_run
from ml.train.evaluate import evaluate, percentile_rank
from ml.validation.known_events import check_known_events
from ops.redact import redact_text

DEFAULT_CONTAMINATION = 0.025
DEFAULT_N_ESTIMATORS = 300
DEFAULT_RANDOM_STATE = 42


def train(
    *,
    duckdb_path: str | Path,
    out_dir: str | Path,
    dataset_commit: str | None,
    contamination: float = DEFAULT_CONTAMINATION,
    n_estimators: int = DEFAULT_N_ESTIMATORS,
    random_state: int = DEFAULT_RANDOM_STATE,
    mlflow_uri: str | None = None,
) -> dict[str, Any]:
    """Fit, evaluate, track and persist one model."""
    import duckdb
    from sklearn.ensemble import IsolationForest

    path = Path(duckdb_path)
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found — run `dbt build` first")

    connection = duckdb.connect(str(path), read_only=True)
    try:
        frame = build_fire_features(connection)
    finally:
        connection.close()

    # `feature_matrix` drops rows without a full history; keep exactly those rows so
    # the evaluation frame and the fitted matrix stay row-aligned.
    features, _keys = feature_matrix(frame)
    matrix = features.reset_index(drop=True)
    scored = frame.loc[features.index].reset_index(drop=True)

    model = IsolationForest(
        n_estimators=n_estimators,
        contamination=contamination,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(matrix)

    # Higher = more anomalous. score_samples is a log density; negating puts the
    # direction the rest of the pipeline expects.
    raw_scores = -model.score_samples(matrix)
    percentiles = percentile_rank(raw_scores, raw_scores)
    threshold = float(np.quantile(percentiles, 1.0 - contamination))

    scored = scored.assign(
        anomaly_score=raw_scores,
        anomaly_percentile=percentiles,
        is_anomaly=percentiles >= threshold,
    )

    report = evaluate(
        scored,
        scored["anomaly_score"].to_numpy(),
        reference_flags=scored["is_anomaly"].to_numpy(),
        value_column="detection_count",
        top_fraction=contamination,
    )
    events = check_known_events(scored)
    known_event_pass_rate = (
        sum(1 for outcome in events if outcome.passed) / len(events) if events else 0.0
    )

    bundle = ModelBundle(
        model=model,
        feature_columns=list(FIRE_FEATURE_COLUMNS),
        anomaly_percentile_threshold=threshold,
        contamination=contamination,
        model_kind="isolation_forest_fire",
        trained_on={
            "dataset_commit": dataset_commit,
            "gold_table": "gold.gold_fire_anomalies",
            "rows": int(len(matrix)),
            "regions": sorted(scored["region_id"].unique().tolist()),
            "first_day": str(pd.to_datetime(scored["observation_date"]).min().date()),
            "last_day": str(pd.to_datetime(scored["observation_date"]).max().date()),
            "reference_rule": "median/MAD z-score in gold.gold_fire_anomalies",
        },
        n_estimators=n_estimators,
        random_state=random_state,
        score_reference=raw_scores.tolist(),
        # Carry the full measured summary, including the known-event result, so the
        # generated model card cannot claim something the run did not observe.
        evaluation={
            **{key: float(value) for key, value in report.metrics.items()},
            "known_event_pass_rate": float(known_event_pass_rate),
            "known_event_caught": float(sum(1 for outcome in events if outcome.passed)),
            "known_event_total": float(len(events)),
        },
    )

    out_path = Path(out_dir)
    artifact = save_bundle(bundle, out_path)
    scored.to_parquet(out_path / "scored_training_rows.parquet", index=False)

    metrics = {
        **{key: float(value) for key, value in report.metrics.items()},
        "known_event_pass_rate": float(known_event_pass_rate),
        "training_rows": float(len(matrix)),
        "feature_count": float(len(FIRE_FEATURE_COLUMNS)),
    }

    with tracked_run(
        "isolation_forest_fire",
        dataset_commit=dataset_commit,
        tracking_uri=mlflow_uri,
    ) as run:
        run.log_params(
            {
                "model_kind": bundle.model_kind,
                "n_estimators": n_estimators,
                "contamination": contamination,
                "random_state": random_state,
                "feature_columns": ",".join(FIRE_FEATURE_COLUMNS),
                "training_rows": len(matrix),
                "first_day": bundle.trained_on["first_day"],
                "last_day": bundle.trained_on["last_day"],
                "regions": ",".join(bundle.trained_on["regions"]),  # type: ignore[arg-type]
            }
        )
        run.log_metrics(metrics)
        run.log_artifact(artifact)
        run.log_artifact(out_path / "scored_training_rows.parquet")
        run.set_tags({"gold_table": "gold_fire_anomalies"})
        tracking_backend = run.backend
        tracking_run_id = run.run_id

    report_payload = {
        "artifact": str(artifact),
        "rows": int(len(matrix)),
        "feature_count": len(FIRE_FEATURE_COLUMNS),
        "anomaly_percentile_threshold": threshold,
        "contamination": contamination,
        "metrics": metrics,
        "notes": report.notes,
        "known_events": [outcome.as_dict() for outcome in events],
        "dataset_commit": dataset_commit,
        "tracking_backend": tracking_backend,
        "tracking_run_id": tracking_run_id,
        "trained_on": bundle.trained_on,
    }
    # Persisted so CI can confirm the validation actually ran, rather than inferring it
    # from an absence of failures.
    (out_path / "training_report.json").write_text(
        json.dumps(report_payload, indent=2, default=str), encoding="utf-8"
    )
    return report_payload


def resolve_dataset_commit(explicit: str | None) -> str | None:
    """Use the given commit, else read the current bronze revision from the Hub."""
    if explicit:
        return explicit
    try:
        from storage.hf_dataset_reader import HFDatasetReader

        return HFDatasetReader(repo_id=hf_repo("bronze")).commit_sha()
    except Exception as exc:  # noqa: BLE001 - lineage is required, but not by this path
        print(f"could not resolve the bronze commit hash: {redact_text(exc)}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duckdb-path", default=os.environ.get("DUCKDB_PATH", "transform/terrasentinel.duckdb"))
    parser.add_argument("--out-dir", default="ml/artifacts/isolation_forest_fire")
    parser.add_argument(
        "--dataset-commit",
        default=os.environ.get("HF_DATASET_COMMIT"),
        help="bronze repo revision the features came from (default: read from the Hub)",
    )
    parser.add_argument("--contamination", type=float, default=DEFAULT_CONTAMINATION)
    parser.add_argument("--n-estimators", type=int, default=DEFAULT_N_ESTIMATORS)
    parser.add_argument("--mlflow-uri", default=None)
    parser.add_argument(
        "--require-known-events",
        action="store_true",
        help="exit non-zero if any documented real event is missed",
    )
    args = parser.parse_args(argv)

    commit = resolve_dataset_commit(args.dataset_commit)
    if not commit:
        print(
            "no dataset commit available; set --dataset-commit or HF_TOKEN so the bronze "
            "revision can be read. Refusing to train an untraceable model.",
            file=sys.stderr,
        )
        return 2

    try:
        report = train(
            duckdb_path=args.duckdb_path,
            out_dir=args.out_dir,
            dataset_commit=commit,
            contamination=args.contamination,
            n_estimators=args.n_estimators,
            mlflow_uri=args.mlflow_uri,
        )
    except FileNotFoundError as exc:
        print(redact_text(exc), file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, default=str))

    if args.require_known_events:
        missed = [event for event in report["known_events"] if not event["passed"]]
        if missed:
            print(f"\n{len(missed)} documented event(s) not detected", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
