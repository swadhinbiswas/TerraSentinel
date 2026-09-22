"""Publish a trained model to the Hugging Face model registry.

The registry is a *model* repo rather than a dataset repo because repo type is part of
the address on the Hub and model cards and revisions are model-repo features — a model
cannot live inside the lake repo, however convenient that would be.

The card is generated from the bundle rather than written by hand, so it cannot drift
from what was actually measured. Every number in it — including the agreement with the
statistical rule — comes from ``bundle.evaluation``, which training computes against
``gold_fire_anomalies.is_anomaly`` (an independent reference), never against the
model's own flags. When agreement is near-total the card says so as a measured
limitation; when it is not, the card reports the real overlap.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from collectors.config import get_secret, hf_repo
from ml.bundle import ModelBundle, load_bundle
from ops.redact import redact_text

CARD_FILENAME = "README.md"

#: How each known-event criterion reads on the card. A failed check appends the run's
#: own verdict, so a miss is spelled out rather than rounded off.
CRITERION_PHRASES: dict[str, str] = {
    "flag": "caught by the serving threshold",
    "rank": "caught by rank only, below the serving threshold",
    "clean": "correctly not flagged (negative control)",
    "missed": "missed",
    "data-changed": "reported as a data change, not a model result",
    "no-data": "no data for this window",
}


def _outcome_phrase(outcome: dict[str, Any]) -> str:
    """One line's verdict, honest about failures."""
    criterion = str(outcome.get("criterion") or "unknown")
    phrase = CRITERION_PHRASES.get(criterion, criterion)
    if outcome.get("passed"):
        return phrase
    return f"**FAILED** — {phrase}: {outcome.get('verdict')}"


def render_model_card(bundle: ModelBundle) -> str:
    """Generate the model card from the bundle's own recorded measurements."""
    trained = bundle.trained_on
    evaluation = bundle.evaluation

    def metric(name: str) -> str:
        value = evaluation.get(name)
        if value is None:
            return "n/a"
        return f"{value:.4f}" if isinstance(value, float) else str(value)

    def number(name: str) -> float | None:
        value = evaluation.get(name)
        return float(value) if isinstance(value, (int, float)) else None

    # What the comparison was against, stated outright: the reader has to know the
    # reference is the gold mart's own flag and not the model's own output.
    precision = number("precision_vs_reference")
    jaccard = number("jaccard")
    recall = number("reference_recall")
    recall_text = f"{recall:.4f}" if recall is not None else "n/a"
    reference_note = ""
    if precision is not None and jaccard is not None:
        selected_days = int(evaluation.get("model_selected", 0))
        reference_days = int(evaluation.get("reference_flagged", 0))
        overlap_days = int(evaluation.get("overlap", 0))
        reference_note = (
            "\n**Reference agreement.** The comparator is `gold_fire_anomalies.is_anomaly` "
            "— the independent median/MAD rule (z ≥ 5) — not this model's own flags, so the "
            f"numbers below can disagree and the model can be wrong. It flags {selected_days} "
            f"days, the rule flags {reference_days}, they share {overlap_days}: precision "
            f"{precision:.4f}, reference recall {recall_text}, Jaccard {jaccard:.4f}.\n"
        )

    overlap_note = ""
    if precision is not None and jaccard is not None and precision >= 0.99 and jaccard >= 0.99:
        overlap_days = int(evaluation.get("overlap", 0))
        selected_days = int(evaluation.get("model_selected", 0))
        separation = metric("detection_count_ratio")
        overlap_note = (
            "\n**Measured limitation — read this before using the score.** At the serving "
            "threshold the model's flagged slice is effectively *identical* to the statistical "
            "median/MAD rule in `gold_fire_anomalies` "
            f"({overlap_days} of {selected_days} days, Jaccard {jaccard:.4f}), measured against "
            f"the rule's own flag rather than against the model. The highest days are ~{separation}x "
            "the rest, so they are trivially separable and every method finds the same ones. "
            "This model therefore adds **no information** over the rule on this dataset; it is "
            "a working baseline and a registry/scoring vehicle, not an improvement. Its value "
            "would appear where the rule is blind — fusing independent signals, or a lower "
            "flag rate — which needs the Sentinel deforestation series that is not yet "
            "backfilled.\n"
        )
    # Prose between the evaluation table and the next heading, each block separated by a
    # blank line so the Markdown renders whether or not a block is present.
    notes = "".join(part for part in (reference_note, overlap_note) if part)
    separable_bullet = (
        "- **The top slice is trivially separable** (see the measured limitation above).\n"
        if overlap_note
        else ""
    )

    caught = int(evaluation.get("known_event_caught") or 0)
    total = int(evaluation.get("known_event_total") or 0)
    known_event_row = (
        f"{metric('known_event_pass_rate')} ({caught}/{total} documented events)"
        if total
        else "n/a"
    )

    # The documented-events list is rendered from the run's own outcomes. A hardcoded
    # list would claim "caught by flag" for an event the next training run missed.
    outcomes = evaluation.get("known_events")
    if isinstance(outcomes, list) and outcomes:
        rank_only = sum(1 for outcome in outcomes if outcome.get("criterion") == "rank")
        events_intro = (
            "`ml/validation/known_events.py` holds real events with **measured** signatures. "
            f"{rank_only} of {len(outcomes)} were caught only by rank (below the serving "
            "threshold), which is reported rather than hidden:"
            if rank_only
            else f"`ml/validation/known_events.py` holds {len(outcomes)} checks against real "
            "events with **measured** signatures. None relied on rank alone:"
        )
        event_lines = "\n".join(
            f"- {outcome.get('region_id')} {outcome.get('window')}, peak "
            f"**{int(outcome.get('max_detections') or 0):,}** — "
            f"{_outcome_phrase(outcome)}"
            for outcome in outcomes
        )
    else:
        events_intro = (
            "This bundle records no per-event outcomes, so nothing is claimed about them here."
        )
        event_lines = ""

    return f"""---
license: apache-2.0
library_name: scikit-learn
tags: [anomaly-detection, isolation-forest, geospatial, wildfire, terra-sentinel]
---

# {bundle.model_kind} `{bundle.version}`

Unsupervised anomaly detector for daily fire activity over the TerraSentinel study
regions. Trained with `IsolationForest` on **{len(bundle.feature_columns)} strictly causal
features** — every rolling window ends at `1 preceding`, so no feature can see the day it
scores.

## Provenance

| | |
|---|---|
| Dataset commit | `{bundle.dataset_commit}` |
| Gold table | `{trained.get('gold_table')}` |
| Rows | {trained.get('rows')} |
| Regions | {', '.join(trained.get('regions') or [])} |
| Date range | {trained.get('first_day')} → {trained.get('last_day')} |
| Reference rule | {trained.get('reference_rule')} |
| Trained at | {bundle.trained_at} |
| n_estimators | {bundle.n_estimators} |
| Contamination | {bundle.contamination} |

The dataset commit is the point of this table: it is what lets you answer "which data
produced this model".

## Evaluation

No labels exist, so accuracy and F1 are not reported — they would be fabricated. What
was measured:

| Metric | Value |
|---|---|
| Flagged days (contamination budget) | {metric('model_selected')} |
| Mean detections, flagged slice | {metric('detection_count_flagged_mean')} |
| Mean detections, rest | {metric('detection_count_rest_mean')} |
| Ratio (separation) | {metric('detection_count_ratio')}x |
| Agreement with the median/MAD rule | {metric('precision_vs_reference')} |
| Jaccard with the rule | {metric('jaccard')} |
| Score mass in the top slice | {metric('score_mass_in_top_slice')} |
| Known-event regression | {known_event_row} |
{notes}
## Documented events used as a regression test

{events_intro}

{event_lines}

## Limitations

- **{len(trained.get('regions') or [])} regions, {trained.get('first_day')} → {trained.get('last_day')}.**
  {int(trained.get('rows') or 0):,} training rows. This is a baseline, not a production dataset.
- **No labels.** Every number above is distributional or agreement-based. Nothing here
  is a precision or recall against truth.
{separable_bullet}- Instrument FRP is not comparable across sensors: MODIS mean FRP is ~100 MW where
  VIIRS is ~15 MW for the same fires. The model consumes `frp_sum` and `frp_per_detection`
  pooled across instruments, so intensity features carry an instrument-mix confound.
- The percentile score is relative to the *training* distribution. A genuinely new
  regime (a year far outside the training range) will saturate the percentile.

## Usage

```python
from ml.bundle import load_bundle, score_to_percentile

bundle = load_bundle("path/or/hub/snapshot")
raw = -bundle.model.score_samples(X[bundle.feature_columns])
percentiles, comparable = score_to_percentile(bundle, raw)
```

Anomaly is `percentile >= {bundle.anomaly_percentile_threshold:.4f}`.

## Attribution

Fire detections: NASA FIRMS (MODIS and VIIRS active fire products). Sea-surface
temperature precursor: NOAA OISST v2.1. Imagery (not used by this model, but by the
pipeline): Copernicus Sentinel.
"""


def push(
    *,
    bundle_dir: str | Path,
    repo_id: str | None = None,
    token: str | None = None,
    revision: str = "main",
) -> dict[str, Any]:
    """Upload the bundle and its generated card to the model repo."""
    from huggingface_hub import HfApi, create_repo

    bundle = load_bundle(bundle_dir)
    resolved_repo = repo_id or hf_repo("models")
    resolved_token = token or get_secret("HF_TOKEN")
    api = HfApi(token=resolved_token)

    create_repo(resolved_repo, repo_type="model", exist_ok=True, private=False, token=resolved_token)

    source = Path(bundle_dir)
    card_path = source / CARD_FILENAME
    card_path.write_text(render_model_card(bundle), encoding="utf-8")

    summary_path = source / "bundle_summary.json"
    if not summary_path.is_file():
        summary_path.write_text(json.dumps(bundle.describe(), indent=2, default=str), encoding="utf-8")

    api.upload_folder(
        folder_path=str(source),
        repo_id=resolved_repo,
        repo_type="model",
        revision=revision,
        token=resolved_token,
        commit_message=(
            f"model {bundle.scored_label()} trained on dataset commit "
            f"{(bundle.dataset_commit or 'unknown')[:12]}"
        ),
    )
    info = api.repo_info(resolved_repo, repo_type="model", revision=revision)
    return {
        "repo_id": resolved_repo,
        "url": f"https://huggingface.co/{resolved_repo}",
        "revision": info.sha,
        "model_kind": bundle.model_kind,
        "model_version": bundle.version,
        "scored_label": bundle.scored_label(),
        "dataset_commit": bundle.dataset_commit,
        "files": sorted(path.name for path in source.rglob("*") if path.is_file()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle-dir", default="ml/artifacts/isolation_forest_fire")
    parser.add_argument("--repo-id", default=None, help="defaults to HF_NAMESPACE/TerraSentinel-models")
    parser.add_argument("--print-card", action="store_true", help="render the card without uploading")
    args = parser.parse_args(argv)

    if args.print_card:
        print(render_model_card(load_bundle(args.bundle_dir)))
        return 0

    try:
        report = push(bundle_dir=args.bundle_dir, repo_id=args.repo_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear CLI failure
        print(f"could not publish the model: {redact_text(exc)}", file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
