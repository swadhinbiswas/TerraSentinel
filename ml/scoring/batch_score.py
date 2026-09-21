"""Apply the registered model to the latest gold features.

This runs in the scheduled job, not on the request path. The dashboard reads
precomputed scores from the serving database, which is what keeps Pages Functions
inside their CPU budget and makes a dashboard read a single indexed query.

Two sinks, and the distinction matters:

* **`ml_predictions` in the serving database** (Turso) — what the dashboard reads. The
  table is owned by the ML layer, not by dbt, so a gold-mart sync can never blank a
  score that scoring has written.
* **A local parquet artifact** — always written, so a run is inspectable and testable
  without a database. `--dry-run` stops there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from collectors.config import get_secret
from ml.bundle import ModelBundle, load_bundle, score_to_percentile
from ops.redact import redact_text

PREDICTIONS_TABLE = "ml_predictions"

#: Composite key for idempotent upserts: one score per region, day and model version.
PREDICTIONS_KEY: tuple[str, ...] = ("region_id", "observation_date", "model_version")

PREDICTIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS {PREDICTIONS_TABLE} (
    region_id     TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    model_version TEXT NOT NULL,
    anomaly_score REAL,
    anomaly_percentile REAL,
    is_anomaly    INTEGER,
    threshold     REAL,
    dataset_commit TEXT,
    region_kind   TEXT,
    scored_at     TEXT NOT NULL,
    PRIMARY KEY (region_id, observation_date, model_version)
)
""".strip()


def latest_model_dir(local_dir: str | Path | None = None, *, revision: str = "main") -> Path:
    """Resolve a model bundle: a local directory if given, else a Hub snapshot."""
    if local_dir is not None and Path(local_dir).is_dir():
        return Path(local_dir)

    from collectors.config import hf_repo
    from storage.hf_dataset_reader import HFDatasetReader

    repo = hf_repo("models")
    target = Path("ml/artifacts/registry") / repo.replace("/", "__")
    reader = HFDatasetReader(repo_id=repo, repo_kind="models", revision=revision)
    return reader.snapshot(target)


def score_frame(frame: pd.DataFrame, bundle: ModelBundle) -> pd.DataFrame:
    """Score an already-built feature frame, preserving row alignment."""
    missing = [column for column in bundle.feature_columns if column not in frame.columns]
    if missing:
        raise KeyError(
            f"feature frame is missing {missing}; the bundle expects "
            f"{len(bundle.feature_columns)} columns and the table has changed since training"
        )

    matrix = frame[bundle.feature_columns].fillna(0.0)
    raw = -bundle.model.score_samples(matrix)
    percentiles, comparable = score_to_percentile(bundle, raw)

    scored = frame.copy()
    scored["anomaly_score"] = raw
    scored["anomaly_percentile"] = percentiles
    scored["is_anomaly"] = percentiles >= bundle.anomaly_percentile_threshold
    scored.attrs["percentile_is_comparable_to_training"] = comparable
    return scored


def predictions_from(scored: pd.DataFrame, bundle: ModelBundle) -> pd.DataFrame:
    """Shape scored rows into the serving table's columns."""
    scored_at = pd.Timestamp.now(tz="UTC").isoformat()
    return pd.DataFrame(
        {
            "region_id": scored["region_id"],
            "observation_date": pd.to_datetime(scored["observation_date"]).dt.date.astype(str),
            "model_version": bundle.scored_label(),
            "anomaly_score": scored["anomaly_score"].astype(float),
            "anomaly_percentile": scored["anomaly_percentile"].astype(float),
            "is_anomaly": scored["is_anomaly"].fillna(False).astype(int),
            "threshold": float(bundle.anomaly_percentile_threshold),
            "dataset_commit": bundle.dataset_commit,
            "region_kind": "fire",
            "scored_at": scored_at,
        }
    )


def write_predictions(
    predictions: pd.DataFrame,
    *,
    dry_run: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    """Upsert into the serving database when configured; always report what happened."""
    if dry_run:
        return {"written": 0, "sink": "dry-run", "rows": len(predictions)}

    if client is None:
        configured = bool(get_secret("TURSO_DATABASE_URL", required=False)) and bool(
            get_secret("TURSO_AUTH_TOKEN", required=False)
        )
        if not configured:
            return {
                "written": 0,
                "sink": "none",
                "rows": len(predictions),
                "note": "TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set; scores were not published",
            }
        from storage.turso_client import TursoClient

        client = TursoClient()

    client.execute(PREDICTIONS_DDL)
    written = client.upsert(
        PREDICTIONS_TABLE,
        predictions.to_dict(orient="records"),
        key_columns=list(PREDICTIONS_KEY),
    )
    return {"written": written, "sink": "turso", "rows": len(predictions)}


def batch_score(
    *,
    duckdb_path: str | Path,
    model_dir: str | Path | None = None,
    out: str | Path = "ml/artifacts/predictions.parquet",
    dry_run: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    import duckdb

    path = Path(duckdb_path)
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found — run `dbt build` first")

    resolved = latest_model_dir(model_dir)
    bundle = load_bundle(resolved)

    # Features are rebuilt with the same causal windows used at training time — the
    # scoring path must construct the feature vector identically or the model sees a
    # silently different input distribution.
    from ml.features.build_feature_table import build_fire_features

    connection = duckdb.connect(str(path), read_only=True)
    try:
        features = build_fire_features(connection)
    finally:
        connection.close()

    scored = score_frame(features, bundle)
    predictions = predictions_from(scored, bundle)

    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_parquet(destination, index=False)

    write_report = write_predictions(predictions, dry_run=dry_run, client=client)

    return {
        "model_dir": str(resolved),
        "model_version": bundle.scored_label(),
        "dataset_commit": bundle.dataset_commit,
        "rows_scored": int(len(scored)),
        "flagged": int(predictions["is_anomaly"].sum()),
        "flag_rate": round(float(predictions["is_anomaly"].mean()), 4),
        "percentile_comparable_to_training": bool(
            scored.attrs.get("percentile_is_comparable_to_training", False)
        ),
        "artifact": str(destination),
        "publish": write_report,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duckdb-path", default=os.environ.get("DUCKDB_PATH", "transform/terrasentinel.duckdb"))
    parser.add_argument("--model-dir", default=None, help="local bundle dir (default: snapshot from the Hub)")
    parser.add_argument("--out", default="ml/artifacts/predictions.parquet")
    parser.add_argument("--dry-run", action="store_true", help="score and write locally, publish nothing")
    args = parser.parse_args(argv)

    try:
        report = batch_score(
            duckdb_path=args.duckdb_path,
            model_dir=args.model_dir,
            out=args.out,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, KeyError) as exc:
        print(redact_text(exc), file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
