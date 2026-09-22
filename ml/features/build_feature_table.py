"""Build the wide per-region-per-day feature table the anomaly models train on.

The rule that governs this file: **every feature must be computable from data that
existed before the day being scored.** A rolling mean that includes today leaks the
target into the input, and an unsupervised model will happily learn to reproduce it —
producing something that looks excellent offline and is worthless live. Every window
in the SQL below therefore ends at ``1 preceding``.

The statistical score in the gold mart (``zscore``, ``baseline_*``) is deliberately
**not** used as a feature. Two reasons: it is computed from the whole history
including days after the one being scored, so it is hindsight rather than a causal
signal; and if the model simply relearned it, comparing the two would be circular.
It is instead carried through as *reference metadata* (``zscore`` and the mart's own
``is_anomaly`` flag) so evaluation can compare the model against it. That column is
selected as a feature never: ``feature_matrix`` picks ``FIRE_FEATURE_COLUMNS``
explicitly, and the reference is read after fitting, where it belongs — a weak,
non-ground-truth comparator.

Scope note: with two regions and two years this is ~1,460 rows. That is a baseline,
not a production dataset. The value of the pipeline is that adding regions or years
feeds the same code without changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from ops.redact import redact_text

#: Columns the model consumes. Keys and metadata are excluded so a stray schema
#: addition cannot silently change the feature vector.
FIRE_FEATURE_COLUMNS: tuple[str, ...] = (
    "detection_count",
    "h3_cell_count",
    "high_confidence_count",
    "frp_sum",
    "frp_max",
    "frp_mean",
    "confidence_pct_mean",
    "count_lag1",
    "count_lag2",
    "count_lag3",
    "count_lag7",
    "count_mean_7",
    "count_std_7",
    "count_max_7",
    "count_mean_14",
    "count_max_14",
    "count_mean_30",
    "count_std_30",
    "zero_days_30",
    "count_delta_1",
    "count_ratio_7",
    "count_ratio_30",
    # Context the statistical rule cannot see. Without these the model simply
    # re-derives the rule: measured, its flagged slice was byte-identical to the
    # median/MAD rule's (precision_vs_reference = 1.0, jaccard = 1.0), which is a
    # model that adds nothing. These are the features that give it a chance to
    # disagree for a physical reason.
    "fire_dispersion",          # cells per detection: one large fire vs many small ones
    "frp_per_detection",        # mean intensity, independent of how many were seen
    "high_confidence_share",    # how sure the instruments were
    "sst_anomaly_mean_7",       # marine heatwave precursor, from a different sensor
    "sst_anomaly_change_7",     # is the ocean warming or cooling into this day
)

#: Identity and provenance columns, carried alongside but never fed to the model.
KEY_COLUMNS: tuple[str, ...] = ("region_id", "observation_date")

_FIRE_SQL = """
with sst as (

    -- Exogenous precursor: a trailing 7-day mean of the region's sea-surface
    -- temperature anomaly. This comes from NOAA OISST, a different instrument
    -- entirely, so it is information the fire-count baseline physically cannot
    -- contain. Window ends at 1 preceding, like every other feature here.
    select
        region_id,
        observation_date,
        avg(value) over (
            partition by region_id order by observation_date
            rows between 7 preceding and 1 preceding
        ) as sst_anomaly_mean_7,
        avg(value) over (
            partition by region_id order by observation_date
            rows between 14 preceding and 8 preceding
        ) as sst_anomaly_mean_prior_7
    from (
        select region_id, timezone('UTC', observed_at)::date as observation_date, avg(value) as value
        from staging.stg_noaa
        where metric_type = 'sst_anomaly'
        group by 1, 2
    )

),

base as (

    select
        region_id,
        observation_date,
        detection_count,
        h3_cell_count,
        high_confidence_count,
        coalesce(frp_sum, 0) as frp_sum,
        coalesce(frp_max, 0) as frp_max,
        coalesce(frp_mean, 0) as frp_mean,
        coalesce(confidence_pct_mean, 0) as confidence_pct_mean,
        -- Reference metadata, *not* features: the model must never see these, but
        -- training needs them afterwards so evaluation can compare the model's flags
        -- against the gold mart's own independent flag rather than against itself.
        -- `feature_matrix` selects FIRE_FEATURE_COLUMNS only, so nothing here enters X.
        zscore,
        is_anomaly
    from gold.gold_fire_anomalies
    where zscore is not null          -- first year has no baseline to score against

),

windowed as (

    select
        *,
        lag(detection_count, 1) over w_all as count_lag1,
        lag(detection_count, 2) over w_all as count_lag2,
        lag(detection_count, 3) over w_all as count_lag3,
        lag(detection_count, 7) over w_all as count_lag7,
        avg(detection_count) over w7 as count_mean_7,
        stddev_samp(detection_count) over w7 as count_std_7,
        max(detection_count) over w7 as count_max_7,
        avg(detection_count) over w14 as count_mean_14,
        max(detection_count) over w14 as count_max_14,
        avg(detection_count) over w30 as count_mean_30,
        stddev_samp(detection_count) over w30 as count_std_30,
        sum(case when detection_count = 0 then 1 else 0 end) over w30 as zero_days_30
    from base
    window
        w_all as (partition by region_id order by observation_date),
        w7 as (partition by region_id order by observation_date rows between 7 preceding and 1 preceding),
        w14 as (partition by region_id order by observation_date rows between 14 preceding and 1 preceding),
        w30 as (partition by region_id order by observation_date rows between 30 preceding and 1 preceding)

)

select
    w.region_id,
    w.observation_date,
    * exclude (region_id, observation_date, sst_anomaly_mean_7, sst_anomaly_mean_prior_7),
    w.count_delta_1,
    w.count_ratio_7,
    w.count_ratio_30,
    coalesce(s.sst_anomaly_mean_7, 0.0) as sst_anomaly_mean_7,
    coalesce(s.sst_anomaly_mean_7 - s.sst_anomaly_mean_prior_7, 0.0) as sst_anomaly_change_7,
    sin(2 * pi() * dayofyear(w.observation_date) / 365.25) as doy_sin,
    cos(2 * pi() * dayofyear(w.observation_date) / 365.25) as doy_cos
from (
    select
        *,
        detection_count - count_lag1 as count_delta_1,
        detection_count / (count_mean_7 + 1.0) as count_ratio_7,
        detection_count / (count_mean_30 + 1.0) as count_ratio_30,
        h3_cell_count / greatest(detection_count, 1) as fire_dispersion,
        frp_sum / greatest(detection_count, 1) as frp_per_detection,
        high_confidence_count / greatest(detection_count, 1) as high_confidence_share
    from windowed
) w
left join sst s
    on w.region_id = s.region_id
   and w.observation_date = s.observation_date
order by w.region_id, w.observation_date
"""


def build_fire_features(connection: Any) -> pd.DataFrame:
    """Causal fire-risk features, one row per region per day."""
    frame = connection.execute(_FIRE_SQL).df()
    for column in ("observation_date",):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column])
    return frame


BUILDERS = {"fire": build_fire_features}


def feature_matrix(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split into ``(X, keys)``, dropping rows that lack a full feature vector.

    Leading rows cannot have lags, and the first windowed days lack a 30-day history.
    Dropping them is correct: imputing a lag would invent information.
    """
    missing = [column for column in FIRE_FEATURE_COLUMNS if column not in frame.columns]
    if missing:
        raise KeyError(f"feature frame is missing {missing}; has {list(frame.columns)}")

    usable = frame.dropna(subset=list(FIRE_FEATURE_COLUMNS))
    return usable[list(FIRE_FEATURE_COLUMNS)].copy(), usable[list(KEY_COLUMNS)].copy()


def build(
    *,
    duckdb_path: str | Path,
    kind: str = "fire",
    out: str | Path | None = None,
) -> dict[str, object]:
    """Build a feature table from the gold marts and optionally persist it."""
    import duckdb

    path = Path(duckdb_path)
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found — run `dbt build` first")
    if kind not in BUILDERS:
        raise KeyError(f"unknown feature table {kind!r}; known: {sorted(BUILDERS)}")

    connection = duckdb.connect(str(path), read_only=True)
    try:
        frame = BUILDERS[kind](connection)
    finally:
        connection.close()

    features, keys = feature_matrix(frame)
    report: dict[str, object] = {
        "kind": kind,
        "rows": len(frame),
        "usable_rows": len(features),
        "dropped_for_missing_history": len(frame) - len(features),
        "features": len(FIRE_FEATURE_COLUMNS),
        "regions": sorted(frame["region_id"].unique().tolist()),
        "first_day": str(frame["observation_date"].min().date()),
        "last_day": str(frame["observation_date"].max().date()),
        "null_fraction_max": round(float(frame[list(FIRE_FEATURE_COLUMNS)].isna().mean().max()), 4),
    }
    if out is not None:
        destination = Path(out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(destination, index=False)
        report["written"] = str(destination)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duckdb-path", default=os.environ.get("DUCKDB_PATH", "transform/terrasentinel.duckdb"))
    parser.add_argument("--kind", default="fire", choices=sorted(BUILDERS))
    parser.add_argument("--out", default=None, help="optional parquet destination")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = build(duckdb_path=args.duckdb_path, kind=args.kind, out=args.out)
    except (FileNotFoundError, KeyError) as exc:
        print(redact_text(exc), file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
