"""Unsupervised evaluation: what can honestly be measured without labels.

There is no ground truth here, so accuracy and F1 are off the table. What *can* be
measured, and what this module measures:

* **Distribution shape.** An anomaly score whose distribution is uniform carries no
  information; one with a short upper tail that concentrates on known-energetic days
  does. Reported as quantiles plus a concentration check.
* **Agreement with the statistical rule.** The gold mart's median/MAD z-score is a
  reference baseline, not truth. Reporting how much the model's top slice overlaps it
  turns "the model works" into a number — and a model that overlaps it *completely* is
  also a finding, because it adds nothing.
* **Separation.** Whether the flagged slice is materially different from the rest on
  the raw physical quantity (detections, FRP). A model that flags ordinary days has a
  high score but no signal.
* **Known events.** Documented real events that must be caught. That lives in
  ``ml/validation/known_events.py`` and is the falsifiable part.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

QUANTILES = (0.5, 0.75, 0.9, 0.95, 0.99, 0.999)


@dataclass
class EvaluationReport:
    metrics: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics, "notes": self.notes}


def score_distribution(scores: np.ndarray) -> dict[str, float]:
    values = np.asarray(scores, dtype=float)
    if values.size == 0:
        raise ValueError("cannot describe an empty score array")
    report = {"score_min": float(values.min()), "score_max": float(values.max())}
    for quantile in QUANTILES:
        report[f"score_p{int(quantile * 1000)}"] = float(np.quantile(values, quantile))
    return report


def percentile_rank(reference: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Percentile of each value within a reference distribution, in ``(0, 1]``.

    Used to convert an unbounded model output into a stable, interpretable score.
    """
    reference = np.sort(np.asarray(reference, dtype=float))
    values = np.asarray(values, dtype=float)
    if reference.size == 0:
        raise ValueError("reference distribution is empty")
    ranks = np.searchsorted(reference, values, side="right")
    return np.clip(ranks / reference.size, 1.0 / reference.size, 1.0)


def agreement_with_reference(
    model_scores: np.ndarray,
    reference_flags: np.ndarray,
    *,
    top_fraction: float,
) -> dict[str, float]:
    """Overlap between the model's top slice and the reference rule's flags.

    ``reference_flags`` is a weak comparator, never ground truth. Two numbers matter:
    precision (of the days the model flags, how many the rule also flags) and the
    reference recall (of the days the rule flags, how many the model catches). A model
    with high precision but near-zero reference recall is just re-deriving the rule.
    """
    scores = np.asarray(model_scores, dtype=float)
    flags = np.asarray(reference_flags, dtype=bool)
    if scores.size != flags.size:
        raise ValueError(f"size mismatch: {scores.size} scores vs {flags.size} flags")

    cutoff = np.quantile(scores, 1.0 - top_fraction)
    selected = scores >= cutoff
    selected_count = int(selected.sum())
    flagged_count = int(flags.sum())

    overlap = int(np.logical_and(selected, flags).sum())
    return {
        "model_selected": float(selected_count),
        "reference_flagged": float(flagged_count),
        "overlap": float(overlap),
        "precision_vs_reference": float(overlap / selected_count) if selected_count else 0.0,
        "reference_recall": float(overlap / flagged_count) if flagged_count else 0.0,
        "jaccard": float(overlap / max(selected_count + flagged_count - overlap, 1)),
    }


def separation(
    frame: pd.DataFrame,
    model_scores: np.ndarray,
    *,
    value_column: str,
    top_fraction: float,
) -> dict[str, float]:
    """How different the flagged slice is on the raw physical quantity."""
    scores = np.asarray(model_scores, dtype=float)
    values = frame[value_column].to_numpy(dtype=float)
    cutoff = np.quantile(scores, 1.0 - top_fraction)
    selected = scores >= cutoff

    if not selected.any():
        return {"flagged_mean": 0.0, "rest_mean": float(np.nanmean(values)), "ratio": 0.0}

    flagged_mean = float(np.nanmean(values[selected]))
    rest_mean = float(np.nanmean(values[~selected])) if (~selected).any() else 0.0
    return {
        f"{value_column}_flagged_mean": flagged_mean,
        f"{value_column}_rest_mean": rest_mean,
        f"{value_column}_ratio": float(flagged_mean / rest_mean) if rest_mean else float("inf"),
    }


def concentration(scores: np.ndarray, *, top_fraction: float = 0.025) -> dict[str, float]:
    """Share of total score mass held by the top slice.

    A model with no discrimination spreads mass roughly evenly; a useful one concentrates
    it in a few days. Note the degenerate case: a *constant* score returns 1.0, because
    every point sits at the cutoff — read that as "no discrimination", not as "perfect".
    """
    values = np.asarray(scores, dtype=float)
    if values.size == 0 or values.sum() <= 0:
        return {"score_mass_in_top_slice": 0.0}
    cutoff = np.quantile(values, 1.0 - top_fraction)
    return {"score_mass_in_top_slice": float(values[values >= cutoff].sum() / values.sum())}


def evaluate(
    frame: pd.DataFrame,
    model_scores: np.ndarray,
    *,
    reference_flags: np.ndarray | None = None,
    value_column: str = "detection_count",
    top_fraction: float = 0.025,
) -> EvaluationReport:
    """Assemble the full unsupervised report."""
    report = EvaluationReport()
    report.metrics.update(score_distribution(model_scores))
    report.metrics.update(concentration(model_scores, top_fraction=top_fraction))
    report.metrics.update(
        separation(frame, model_scores, value_column=value_column, top_fraction=top_fraction)
    )
    if reference_flags is not None:
        report.metrics.update(
            agreement_with_reference(model_scores, reference_flags, top_fraction=top_fraction)
        )
        report.notes.append(
            "precision_vs_reference is agreement with the median/MAD z-score rule "
            "(gold_fire_anomalies.is_anomaly), which is a weak comparator, not ground truth. "
            "The reference is the gold mart's own flag captured before training overwrote "
            "is_anomaly with the model's — a model compared against itself would report a "
            "constant 1.0."
        )
        precision = report.metrics.get("precision_vs_reference", 0.0)
        if precision >= 0.99:
            report.notes.append(
                "The model's flagged slice is almost identical to the statistical rule's; "
                "it is re-deriving the baseline rather than adding information."
            )
    report.notes.append(
        "No labelled anomalies exist, so this reports distributional evidence and "
        "agreement — never accuracy or F1."
    )
    return report
