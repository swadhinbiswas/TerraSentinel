"""One-time historical backfill for every source into HF bronze.

Run this **before** the live collectors are scheduled. Anomaly detection is a
comparison against a baseline — "this week's NDVI versus the same week last year",
"fire radiative power versus the seasonal normal" — so without 1-2 years of history
the first anomaly scores are noise dressed up as signal.

Everything lands under a ``backfill/`` prefix so it is unambiguous which rows came
from the historical pull and which from ongoing daily collection. Backfill runs are
also idempotent: partitions are keyed by observation period, so re-running a window
overwrites the same paths instead of duplicating rows.

Usage::

    # what would this cost?
    python -m collectors.backfill_historical --sources all --plan

    # dry run: fetch and validate, write locally, upload nothing
    python -m collectors.backfill_historical --start-date 2024-09-01 --dry-run

    # the real thing
    python -m collectors.backfill_historical --sources firms,sentinel,noaa_nsidc,entsoe
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, timedelta
from typing import Any

from collectors.base_collector import (
    BaseCollector,
    build_uploader,
    configure_logging,
    parse_date,
    record_run_metadata,
    select_regions,
)
from collectors.config import REGIONS, SOURCES, MissingCredential, preflight_sources
from collectors.entsoe_collector import (
    DOCUMENTS,
    ZONE_BY_REGION,
    EntsoeCollector,
    iter_month_chunks,
)
from collectors.firms_collector import NRT_MAX_AGE_DAYS, SP_MIN_AGE_DAYS, FirmsCollector
from collectors.noaa_nsidc_collector import NoaaNsidcCollector
from collectors.sentinel_gee_collector import SentinelCollector
from collectors.time_utils import composite_windows, iter_date_windows
from ops.redact import redact_text

LOGGER = logging.getLogger("terrasentinel.backfill")

WORKFLOW = "backfill_historical"

COLLECTOR_CLASSES: dict[str, type[BaseCollector]] = {
    "firms": FirmsCollector,
    "sentinel": SentinelCollector,
    "noaa_nsidc": NoaaNsidcCollector,
    "entsoe": EntsoeCollector,
}

#: Default history depth. Two years covers a full seasonal cycle twice, which is
#: the minimum for telling an anomaly from an ordinary bad season.
DEFAULT_LOOKBACK_DAYS = 730


def resolve_sources(spec: str) -> list[str]:
    if spec.strip().lower() in {"", "all"}:
        return sorted(COLLECTOR_CLASSES)
    chosen = [value.strip() for value in spec.split(",") if value.strip()]
    unknown = [value for value in chosen if value not in COLLECTOR_CLASSES]
    if unknown:
        raise SystemExit(f"unknown source(s) {unknown}. Known: {sorted(COLLECTOR_CLASSES)}")
    return chosen


def estimate_requests(
    source_id: str,
    regions: list[Any],
    start: date,
    end: date,
) -> dict[str, int]:
    """Rough upstream request count, for planning before spending API quota."""
    if source_id == "firms":
        spec = SOURCES["firms"]
        windows = iter_date_windows(start, end, size_days=FirmsCollector.max_day_range)
        instruments = len(spec.product_pairs) or len(spec.backfill_products)
        # Most windows cost one pass per instrument. Windows inside the SP/NRT
        # overlap can cost a second pass if the preferred archive has not caught up
        # yet, so they are counted twice rather than under-promising.
        today = date.today()
        overlap = sum(
            1
            for _, window_end in windows
            if SP_MIN_AGE_DAYS <= (today - window_end).days <= NRT_MAX_AGE_DAYS
        )
        return {
            "windows": len(windows),
            "instruments": instruments,
            "overlap_windows": overlap,
            "region_count": len(regions),
            "estimated_requests": (len(windows) + overlap) * instruments * len(regions),
        }
    if source_id == "sentinel":
        months = len(composite_windows(start, end, "M"))
        return {
            "windows": months,
            "region_count": len(regions),
            "estimated_requests": len(regions) * (1 + 4),  # 1 series call + ~4 grid chunks
        }
    if source_id == "entsoe":
        # Calendar-month chunks x mapped zones x the two documents (A44, A65).
        # Regions without a bidding zone contribute zero zones, so an unmapped
        # selection is honestly estimated at zero requests.
        chunks = len(list(iter_month_chunks(start, end)))
        zones = sum(len(ZONE_BY_REGION.get(region.region_id, ())) for region in regions)
        return {
            "windows": chunks,
            "zones": zones,
            "region_count": len(regions),
            "estimated_requests": chunks * zones * len(DOCUMENTS),
        }
    return {
        "windows": len(iter_date_windows(start, end, size_days=7)),
        "region_count": len(regions),
        "estimated_requests": len(regions),
    }


def build_collector(
    source_id: str,
    *,
    dry_run: bool,
    include_climatology: bool,
    max_requests: int | None,
    run_id: str | None = None,
) -> BaseCollector:
    overrides: dict[str, Any] = {}
    if source_id == "firms" and max_requests:
        overrides["max_requests"] = max_requests
    if source_id == "noaa_nsidc":
        overrides["include_climatology"] = include_climatology
    return COLLECTOR_CLASSES[source_id](
        uploader=build_uploader(dry_run=dry_run),
        dry_run=dry_run,
        run_id=run_id,
        **overrides,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", default="all", help="comma list or 'all' (default: all)")
    parser.add_argument(
        "--start-date", help=f"inclusive start (default: today - {DEFAULT_LOOKBACK_DAYS}d)"
    )
    parser.add_argument("--end-date", help="inclusive end (default: today)")
    parser.add_argument("--regions", default="all", help="comma list or 'all'")
    parser.add_argument("--plan", action="store_true", help="print the request plan and exit")
    parser.add_argument("--dry-run", action="store_true", help="write locally, skip HF upload")
    parser.add_argument(
        "--no-climatology",
        action="store_false",
        dest="include_climatology",
        help="skip the static NSIDC 1981-2010 baseline (pulled by default)",
    )
    parser.add_argument("--max-requests", type=int, default=None, help="per-source request cap")
    parser.add_argument("--skip-preflight", action="store_true", help="do not verify credentials first")
    parser.add_argument("--no-metadata", action="store_true", help="do not write pipeline_runs rows")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-json", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(args.log_level, json_output=True if args.log_json else None)

    end = parse_date(args.end_date, field_name="end-date") or date.today()
    start = parse_date(args.start_date, field_name="start-date") or (
        end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    )
    if start > end:
        raise SystemExit(f"--start-date {start} is after --end-date {end}")

    sources = resolve_sources(args.sources)
    plan: dict[str, Any] = {"start": start.isoformat(), "end": end.isoformat(), "sources": {}}
    selected: dict[str, list[Any]] = {}

    for source_id in sources:
        spec = SOURCES[source_id]
        if args.regions.strip().lower() in {"", "all"} and COLLECTOR_CLASSES[source_id].default_region_ids:
            regions = [
                region
                for region in REGIONS
                if region.region_id in COLLECTOR_CLASSES[source_id].default_region_ids
            ]
        else:
            regions = list(select_regions(spec.anomaly_types, args.regions))
        selected[source_id] = regions
        plan["sources"][source_id] = {
            "regions": [region.region_id for region in regions],
            **estimate_requests(source_id, regions, start, end),
        }

    if args.plan:
        print(json.dumps(plan, indent=2))
        return 0

    if not args.skip_preflight:
        try:
            preflight_sources(sources)
        except MissingCredential as exc:
            LOGGER.error("preflight failed:\n%s", redact_text(exc))
            return 2

    outcomes: dict[str, Any] = {}
    for source_id in sources:
        collector = build_collector(
            source_id,
            dry_run=args.dry_run,
            include_climatology=args.include_climatology,
            max_requests=args.max_requests,
        )
        summary = collector.run(selected[source_id], start_date=start, end_date=end, backfill=True)
        record_run_metadata(
            workflow=WORKFLOW,
            summary=summary,
            enabled=not args.no_metadata,
            dry_run=args.dry_run,
        )
        outcomes[source_id] = {
            "status": "failed" if summary.failed and len(summary.failed) == len(summary.results) else (
                "partial" if summary.failed else "success"
            ),
            "rows_written": summary.total_rows,
            "failed_units": len(summary.failed),
            "regions": [
                {"region_id": item.region_id, "status": item.status, "rows": item.rows}
                for item in summary.results
            ],
        }

    print(json.dumps({"plan": plan, "outcomes": outcomes}, indent=2, default=str))

    failed_sources = [name for name, value in outcomes.items() if value["status"] == "failed"]
    if len(failed_sources) == len(outcomes):
        return 2
    if failed_sources:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
