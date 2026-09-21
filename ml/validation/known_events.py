"""Documented real events the model must catch — and ordinary days it must not.

This is the falsifiable part of an unsupervised pipeline. "The model runs" is not
evidence; "the model flags the August 2025 Iberian megafire cluster and does not flag an
ordinary July day" is. Every expectation below was **measured from the gold mart**, not
asserted — the first draft of this file contained an invented number for 2025-07-26
("800 detections") when the real value was 172 and the day's z-score was *negative*.
The check caught it, which is the design working, but the lesson is that expectations
must be traceable to a measurement.

Negative controls matter as much as positive ones. A model that flagged every day would
pass a list of positives, so one ordinary high-summer day is included that must stay
unflagged.

Two criteria are reported separately, because they answer different questions:

* **Operational flag** — is the day in the model's flagged slice at the serving
  threshold (currently the top 2.5%)? This is a rate choice and will be tuned.
* **Rank** — did the day land in the model's top decile? This is the regression
  criterion, and it is the one that stays valid when the serving threshold changes.

A documented event counts as caught if either holds, and the verdict says which.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

#: Rank at or above which a day counts as caught regardless of the serving threshold.
DEFAULT_RANK_THRESHOLD = 0.90

#: Guard against an archive revision silently invalidating an expectation: if the peak
#: measured now is below this fraction of the recorded peak, report a data change
#: rather than a model failure, so the two are never conflated.
DATA_CHANGE_TOLERANCE = 0.6


@dataclass(frozen=True)
class KnownEvent:
    region_id: str
    start: date
    end: date
    description: str
    evidence: str
    #: Peak detections measured in this window when the expectation was recorded.
    measured_peak_detections: int
    #: ``False`` marks a negative control that must not be flagged.
    expect_detected: bool = True

    @property
    def min_detections(self) -> int:
        return int(self.measured_peak_detections * DATA_CHANGE_TOLERANCE)


KNOWN_EVENTS: tuple[KnownEvent, ...] = (
    KnownEvent(
        region_id="iberia_fire",
        start=date(2025, 8, 15),
        end=date(2025, 8, 17),
        description="August 2025 Iberian megafire cluster",
        evidence="Measured peak 13,329 detections on 2025-08-15 (z=18.7) against a seasonal "
        "median of 669 for that day-of-year.",
        measured_peak_detections=13329,
    ),
    KnownEvent(
        region_id="greece_fire",
        start=date(2025, 8, 12),
        end=date(2025, 8, 13),
        description="August 2025 Attica/Euboea fire episode",
        evidence="Measured peak 2,005 detections (z=17.5), roughly 14x the seasonal median.",
        measured_peak_detections=2005,
    ),
    KnownEvent(
        region_id="iberia_fire",
        start=date(2026, 2, 24),
        end=date(2026, 2, 27),
        description="February 2026 out-of-season Iberian fire activity",
        evidence="Measured peak 1,184 detections (z=22.5) against a winter median of 66. "
        "The strongest statistical anomaly in the whole record.",
        measured_peak_detections=1184,
    ),
    KnownEvent(
        region_id="iberia_fire",
        start=date(2026, 7, 3),
        end=date(2026, 7, 3),
        description="Early July 2026 Iberian fire surge",
        evidence="Measured peak 2,238 detections (z=18.0), about 14x the July median.",
        measured_peak_detections=2238,
    ),
    KnownEvent(
        region_id="greece_fire",
        start=date(2024, 9, 30),
        end=date(2024, 9, 30),
        description="Late September 2024 Greek fire activity",
        evidence="Measured peak 808 detections (z=12.5). Kept because the statistical rule "
        "calls it extreme while a lag-based model may not — a real disagreement worth "
        "tracking rather than hiding.",
        measured_peak_detections=808,
    ),
    KnownEvent(
        region_id="iberia_fire",
        start=date(2025, 7, 26),
        end=date(2025, 7, 26),
        description="NEGATIVE CONTROL: an ordinary mid-summer Iberian day",
        evidence="Measured peak 172 detections with z=-0.5 — comfortably below the July "
        "daily mean of 616, so it is an unremarkable day. Originally mis-recorded here as a "
        "1,000+ detection event; the measurement corrected it. A model that flags this is "
        "flagging summer, not anomalies.",
        measured_peak_detections=172,
        expect_detected=False,
    ),
)


@dataclass
class EventOutcome:
    event: KnownEvent
    detected: bool
    flagged_days: int
    window_days: int
    max_detections: int
    max_score_percentile: float | None
    criterion: str
    verdict: str

    @property
    def passed(self) -> bool:
        """True when the outcome matches the expectation (positive or negative)."""
        return self.detected == self.event.expect_detected

    def as_dict(self) -> dict[str, object]:
        return {
            "region_id": self.event.region_id,
            "window": f"{self.event.start}..{self.event.end}",
            "description": self.event.description,
            "expect_detected": self.event.expect_detected,
            "detected": self.detected,
            "passed": self.passed,
            "criterion": self.criterion,
            "verdict": self.verdict,
            "flagged_days": self.flagged_days,
            "window_days": self.window_days,
            "max_detections": self.max_detections,
            "measured_peak_detections": self.event.measured_peak_detections,
            "max_score_percentile": self.max_score_percentile,
        }


def check_known_events(
    frame: pd.DataFrame,
    *,
    score_column: str = "anomaly_percentile",
    flag_column: str = "is_anomaly",
    rank_threshold: float = DEFAULT_RANK_THRESHOLD,
    events: tuple[KnownEvent, ...] = KNOWN_EVENTS,
) -> list[EventOutcome]:
    """Evaluate every documented event against the model's output.

    Missing columns raise rather than being skipped — a silently empty check is worse
    than no check.
    """
    required = {"region_id", "observation_date", "detection_count", score_column, flag_column}
    missing = required - set(frame.columns)
    if missing:
        raise KeyError(f"known-events check needs {sorted(missing)}; frame has {list(frame.columns)}")

    working = frame.copy()
    working["observation_date"] = pd.to_datetime(working["observation_date"]).dt.date

    outcomes: list[EventOutcome] = []
    for event in events:
        window = working[
            (working["region_id"] == event.region_id)
            & (working["observation_date"] >= event.start)
            & (working["observation_date"] <= event.end)
        ]
        if window.empty:
            outcomes.append(
                EventOutcome(
                    event=event,
                    detected=False,
                    flagged_days=0,
                    window_days=0,
                    max_detections=0,
                    max_score_percentile=None,
                    criterion="no-data",
                    verdict="no data for this window — was it backfilled?",
                )
            )
            continue

        max_detections = int(window["detection_count"].max())
        flagged = int(window[flag_column].fillna(False).sum())
        max_score = window[score_column].max()
        max_percentile = float(max_score) if pd.notna(max_score) else None

        if not event.expect_detected:
            # Negative control: the whole window must stay unflagged.
            detected = flagged > 0
            criterion = "flag" if detected else "clean"
            verdict = (
                f"FALSE POSITIVE: {flagged} ordinary day(s) flagged "
                f"(peak {max_detections} detections, max percentile {max_percentile:.3f})"
                if detected
                else f"correctly unflagged (peak {max_detections} detections, "
                f"max percentile {max_percentile:.3f})"
                if max_percentile is not None
                else "correctly unflagged"
            )
            outcomes.append(
                EventOutcome(
                    event=event,
                    detected=detected,
                    flagged_days=flagged,
                    window_days=len(window),
                    max_detections=max_detections,
                    max_score_percentile=max_percentile,
                    criterion=criterion,
                    verdict=verdict,
                )
            )
            continue

        if max_detections < event.min_detections:
            verdict = (
                f"data changed: peak {max_detections} detections vs "
                f"{event.measured_peak_detections} recorded — check the archive"
            )
            detected, criterion = False, "data-changed"
        elif flagged > 0:
            detected, criterion = True, "flag"
            verdict = f"detected by the serving threshold ({flagged} flagged day(s), peak {max_detections})"
        elif max_percentile is not None and max_percentile >= rank_threshold:
            detected, criterion = True, "rank"
            verdict = (
                f"detected by rank: percentile {max_percentile:.3f} >= {rank_threshold}, "
                f"but below the serving threshold — the event is not in the flagged slice"
            )
        else:
            detected, criterion = False, "missed"
            verdict = (
                f"MISSED: peak {max_detections} detections, max percentile "
                f"{max_percentile if max_percentile is None else round(max_percentile, 3)}"
            )

        outcomes.append(
            EventOutcome(
                event=event,
                detected=detected,
                flagged_days=flagged,
                window_days=len(window),
                max_detections=max_detections,
                max_score_percentile=max_percentile,
                criterion=criterion,
                verdict=verdict,
            )
        )
    return outcomes
