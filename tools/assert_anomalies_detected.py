"""Assert the pipeline actually detects anomalies it cannot possibly miss.

This is the payoff of `tools/synthetic_bronze --inject-anomaly`: an unsupervised
model with no labels can always claim to work. Injecting a known, grossly obvious
event and asserting the gold marts flag it is falsifiable evidence, and it is the
same check Phase 5 wires into CI — so a change that breaks detection fails the
build instead of quietly shipping a dashboard that never fires.

Two halves, and the second matters as much as the first:

* **Sensitivity** — the injected events are flagged, with the expected severity.
* **Specificity** — the flag rate stays low. A degenerate baseline (say, a scale of
  zero) would flag everything, which passes a naive sensitivity check while being
  completely useless. Hence the noise ceiling.

Events are discovered by their signature rather than by date, so the checker does
not need to know when the generator planted them.

Usage::

    python -m tools.assert_anomalies_detected --duckdb-path transform/terrasentinel.duckdb
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

#: A fire day this far above the seasonal norm is the injected spike, not weather.
FIRE_SPIKE_THRESHOLD = 40
#: NDVI cannot plausibly fall this far year-over-year without clearance.
NDVI_CRASH_THRESHOLD = -0.3
#: A sea-ice excursion this large is the injected record low.
ICE_EXCURSION_ZSCORE = -3.0
#: Ceiling on the share of scored days the statistical test may flag. A test that
#: flags everything would otherwise pass every sensitivity assertion below.
MAX_FIRE_FLAG_RATE = 0.02


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.name, "passed": self.passed, "detail": self.detail}


def _scalar(connection: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> Any:
    row = connection.execute(sql, params or []).fetchone()
    return row[0] if row else None


def run_checks(connection: duckdb.DuckDBPyConnection) -> list[Check]:
    checks: list[Check] = []

    # --- sensitivity ------------------------------------------------------
    fire_hits = _scalar(
        connection,
        """
        select count(*) from gold.gold_fire_anomalies
        where is_anomaly and detection_count >= ?
        """,
        [FIRE_SPIKE_THRESHOLD],
    )
    checks.append(
        Check(
            "fire_spike_detected",
            bool(fire_hits),
            f"{fire_hits} flagged day(s) with >= {FIRE_SPIKE_THRESHOLD} detections",
        )
    )

    ndvi_hits = _scalar(
        connection,
        """
        select count(*) from gold.gold_deforestation_index
        where is_anomaly and ndvi_change <= ?
        """,
        [NDVI_CRASH_THRESHOLD],
    )
    checks.append(
        Check(
            "ndvi_crash_detected",
            bool(ndvi_hits),
            f"{ndvi_hits} flagged month(s) with NDVI change <= {NDVI_CRASH_THRESHOLD}",
        )
    )

    ice_hits = _scalar(
        connection,
        """
        select count(*) from gold.gold_ice_extent_trends
        where is_anomaly and zscore <= ?
        """,
        [ICE_EXCURSION_ZSCORE],
    )
    checks.append(
        Check(
            "ice_excursion_detected",
            bool(ice_hits),
            f"{ice_hits} flagged period(s) with z <= {ICE_EXCURSION_ZSCORE}",
        )
    )

    marine_hits = _scalar(
        connection,
        """
        select count(*) from gold.gold_h3_sst
        where metric_type = 'sst_anomaly' and value >= 2.5
        """,
    )
    checks.append(
        Check(
            "marine_heatwave_present",
            bool(marine_hits),
            f"{marine_hits} grid cell(s) with SST anomaly >= 2.5 C in the map layer",
        )
    )

    # --- specificity ------------------------------------------------------
    scored = _scalar(connection, "select count(*) from gold.gold_fire_anomalies where zscore is not null")
    flagged = _scalar(connection, "select count(*) from gold.gold_fire_anomalies where is_anomaly")
    rate = (flagged / scored) if scored else 0.0
    checks.append(
        Check(
            "fire_flag_rate_is_selective",
            scored > 0 and rate <= MAX_FIRE_FLAG_RATE,
            f"{flagged}/{scored} scored days flagged ({rate:.2%}, ceiling {MAX_FIRE_FLAG_RATE:.0%})",
        )
    )

    # A mart that silently produced nothing would make every check above vacuous.
    empty_marts = [
        mart
        # Only the marts whose sources are backfilled: the Sentinel ones legitimately
        # do not exist yet, and asserting on them would fail for the wrong reason.
        for mart in (
            "gold_fire_anomalies",
            "gold_ice_extent_trends",
            "gold_h3_fire",
            "gold_h3_sst",
        )
        if not _scalar(connection, f"select count(*) from gold.{mart}")
    ]
    checks.append(
        Check(
            "gold_marts_are_populated",
            not empty_marts,
            f"empty: {empty_marts}" if empty_marts else "all core gold marts have rows",
        )
    )

    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duckdb-path", default="transform/terrasentinel.duckdb")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    path = Path(args.duckdb_path)
    if not path.is_file():
        raise SystemExit(f"{path} not found — run `dbt build` first")

    connection = duckdb.connect(str(path), read_only=True)
    try:
        checks = run_checks(connection)
    finally:
        connection.close()

    if args.json:
        print(json.dumps([check.as_dict() for check in checks], indent=2))
    else:
        for check in checks:
            print(f"{'PASS' if check.passed else 'FAIL'}  {check.name}: {check.detail}")

    failed = [check for check in checks if not check.passed]
    if failed:
        print(f"\n{len(failed)} check(s) failed", file=sys.stderr)
        return 1
    print(f"\nall {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
