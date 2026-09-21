"""The artifact contract shared by training, the registry and batch scoring.

One dataclass rather than three loose conventions, because the failure mode when they
drift is silent: a scoring job that loads a model expecting different feature columns
will either error confusingly or, worse, score with a column order that no longer
matches what was fitted.

The score is a **percentile within the training distribution**, not a raw Isolation
Forest output. Raw `decision_function` values are unbounded and only comparable within
one fitted model; a percentile has a stable meaning on a dashboard ("more unusual than
98.5% of history") and survives retraining.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib

ARTIFACT_FILENAME = "model_bundle.joblib"


@dataclass
class ModelBundle:
    """A fitted model plus everything needed to score with it reproducibly."""

    model: Any
    feature_columns: list[str]
    #: Scores at or above this percentile are flagged. Derived from the target
    #: contamination rate rather than picked, so "anomaly" has a stated frequency.
    anomaly_percentile_threshold: float
    contamination: float
    model_kind: str
    #: Anything downstream needs for lineage: dataset commit, row counts, date range.
    trained_on: dict[str, Any] = field(default_factory=dict)
    trained_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    n_estimators: int | None = None
    random_state: int | None = None
    #: Raw training scores, so a percentile computed at serving time means the same
    #: thing it meant at training time. Without this a new batch would be ranked
    #: against itself, and the same day could score differently depending on what
    #: else happened to be in the batch.
    score_reference: list[float] = field(default_factory=list)
    #: Measured evaluation summary, carried so a model card cannot drift from what
    #: was actually observed at training time.
    evaluation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.feature_columns:
            raise ValueError("feature_columns must not be empty")
        if not 0.0 < self.anomaly_percentile_threshold <= 1.0:
            raise ValueError(
                f"anomaly_percentile_threshold must be in (0, 1], got {self.anomaly_percentile_threshold}"
            )
        if not 0.0 < self.contamination < 0.5:
            raise ValueError(f"contamination must be in (0, 0.5), got {self.contamination}")

    @property
    def dataset_commit(self) -> str | None:
        return self.trained_on.get("dataset_commit")

    @property
    def version(self) -> str:
        """Deterministic short identifier for this model.

        Derived from the inputs rather than a timestamp, so retraining on the same
        data with the same parameters yields the same version and a scoring table
        cannot accumulate duplicate rows for one model.
        """
        import hashlib

        digest = hashlib.sha256(
            "|".join(
                [
                    self.model_kind,
                    str(self.dataset_commit),
                    str(sorted(self.feature_columns)),
                    str(self.n_estimators),
                    str(self.random_state),
                ]
            ).encode("utf-8")
        )
        return digest.hexdigest()[:12]

    def scored_label(self) -> str:
        return f"{self.model_kind}:{self.version}"

    def reference_scores(self) -> Any:
        """The training score distribution as a numpy array, or an empty array."""
        import numpy as np

        return np.asarray(self.score_reference, dtype=float)

    def describe(self) -> dict[str, Any]:
        """JSON-safe summary, with the model object replaced by its class name."""
        payload = {key: value for key, value in asdict(self).items() if key != "model"}
        payload["model_class"] = type(self.model).__name__
        payload["dataset_commit"] = self.dataset_commit
        return payload


def save_bundle(bundle: ModelBundle, directory: str | Path) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / ARTIFACT_FILENAME
    joblib.dump(bundle, path)
    # A sidecar card in plain JSON so the artifact is inspectable without joblib.
    (target / "bundle_summary.json").write_text(
        json.dumps(bundle.describe(), indent=2, default=str), encoding="utf-8"
    )
    return path


def score_to_percentile(bundle: ModelBundle, raw_scores: Any) -> Any:
    """Rank new scores against the bundle's training distribution.

    Falls back to ranking within the batch (and says so in the returned flag) when the
    bundle predates the reference field, because silently producing non-comparable
    percentiles is worse than an explicit approximation.
    """
    from ml.train.evaluate import percentile_rank

    reference = bundle.reference_scores()
    if reference.size == 0:
        return percentile_rank(raw_scores, raw_scores), False
    return percentile_rank(reference, raw_scores), True


def load_bundle(path_or_dir: str | Path) -> ModelBundle:
    """Load a bundle from a directory or a direct ``.joblib`` path."""
    candidate = Path(path_or_dir)
    if candidate.is_dir():
        candidate = candidate / ARTIFACT_FILENAME
    if not candidate.is_file():
        raise FileNotFoundError(f"no model bundle at {candidate}")
    bundle = joblib.load(candidate)
    if not isinstance(bundle, ModelBundle):
        raise TypeError(
            f"{candidate} contains {type(bundle).__name__}, not a ModelBundle — "
            "refusing to score with an unrecognised artifact"
        )
    return bundle
