"""The Phase 0 entrypoint: request planning and source/region resolution.

`--plan` exists so the API cost of a two-year backfill is known before spending it,
and so CI can exercise the CLI without credentials or network.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from collectors.backfill_historical import (
    COLLECTOR_CLASSES,
    DEFAULT_LOOKBACK_DAYS,
    build_collector,
    estimate_requests,
    main,
    resolve_sources,
)
from collectors.config import REGIONS, get_region


class TestResolveSources:
    def test_all_expands_to_every_collector(self) -> None:
        assert resolve_sources("all") == sorted(COLLECTOR_CLASSES)

    def test_blank_means_all(self) -> None:
        assert resolve_sources("") == sorted(COLLECTOR_CLASSES)

    def test_explicit_list_is_respected(self) -> None:
        assert resolve_sources("firms,noaa_nsidc") == ["firms", "noaa_nsidc"]

    def test_unknown_source_fails_loudly(self) -> None:
        with pytest.raises(SystemExit, match="unknown source"):
            resolve_sources("firms,landsat")


class TestEstimateRequests:
    def test_two_years_of_firms_is_under_the_documented_transaction_budget(self) -> None:
        # 5-day windows against a 5000-transactions-per-10-minutes limit.
        estimate = estimate_requests(
            "firms",
            [get_region("iberia_fire"), get_region("greece_fire")],
            date(2024, 9, 21),
            date(2026, 9, 21),
        )
        assert estimate["windows"] == 147
        assert estimate["instruments"] == 3
        # Windows inside the SP/NRT overlap are counted twice, because the
        # preferred archive may not have caught up yet.
        assert estimate["overlap_windows"] == 3
        assert estimate["estimated_requests"] == (147 + 3) * 3 * 2
        assert estimate["estimated_requests"] < 1000

    def test_sentinel_cost_is_per_region_not_per_window(self) -> None:
        estimate = estimate_requests(
            "sentinel",
            [get_region("carpathian_deforest")],
            date(2024, 9, 21),
            date(2026, 9, 21),
        )
        assert estimate["windows"] == 25  # calendar months
        assert estimate["estimated_requests"] < 10

    def test_noaa_needs_no_credentials(self) -> None:
        assert estimate_requests("noaa_nsidc", [], date(2024, 1, 1), date(2024, 1, 8))[
            "estimated_requests"
        ] == 0


class TestBuildCollector:
    def test_noaa_collector_gets_climatology_on_backfill(self, tmp_path) -> None:
        collector = build_collector(
            "noaa_nsidc", dry_run=True, include_climatology=True, max_requests=None
        )
        assert collector.include_climatology is True

    def test_firms_request_cap_is_applied(self, monkeypatch) -> None:
        monkeypatch.setenv("FIRMS_MAP_KEY", "test-key-value-1234567890")
        collector = build_collector(
            "firms", dry_run=True, include_climatology=False, max_requests=42, run_id="run-abcdef"
        )
        assert collector.max_requests == 42
        assert collector.run_id == "run-abcdef"


class TestPlanMode:
    def test_plan_prints_regions_and_request_counts_without_network(self, capsys) -> None:
        assert main(["--sources", "all", "--plan"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert set(payload["sources"]) == set(COLLECTOR_CLASSES)
        assert payload["sources"]["firms"]["regions"] == ["iberia_fire", "greece_fire"]
        assert payload["sources"]["sentinel"]["regions"] == [
            "carpathian_deforest",
            "alps_ice",
            "norway_ice",
        ]
        # Region selection is filtered from the REGIONS tuple, so ordering is the
        # declaration order there rather than each collector's own preference.
        assert payload["sources"]["noaa_nsidc"]["regions"] == [
            "iberia_fire",
            "greece_fire",
            "norway_ice",
            "arctic",
            "antarctic",
        ]

    def test_plan_defaults_to_a_two_year_window(self, capsys) -> None:
        main(["--plan"])
        payload = json.loads(capsys.readouterr().out)
        start = date.fromisoformat(payload["start"])
        end = date.fromisoformat(payload["end"])
        assert (end - start).days == DEFAULT_LOOKBACK_DAYS

    def test_plan_honours_explicit_dates_and_regions(self, capsys) -> None:
        main(
            [
                "--plan",
                "--sources",
                "firms",
                "--regions",
                "iberia_fire",
                "--start-date",
                "2025-01-01",
                "--end-date",
                "2025-01-31",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["start"] == "2025-01-01"
        assert payload["end"] == "2025-01-31"
        assert payload["sources"]["firms"]["regions"] == ["iberia_fire"]

    def test_inverted_date_range_is_rejected(self, capsys) -> None:
        with pytest.raises(SystemExit, match="after"):
            main(["--plan", "--start-date", "2025-02-01", "--end-date", "2025-01-01"])

    def test_malformed_date_is_rejected(self) -> None:
        with pytest.raises(SystemExit, match="YYYY-MM-DD"):
            main(["--plan", "--start-date", "01/02/2025"])

    def test_missing_credentials_fail_preflight_with_a_complete_list(
        self, monkeypatch, capsys
    ) -> None:
        for name in ("FIRMS_MAP_KEY", "GEE_SERVICE_ACCOUNT_JSON", "GEE_PROJECT", "GEE_SERVICE_ACCOUNT_EMAIL"):
            monkeypatch.delenv(name, raising=False)
        # Sentinel is checked first alphabetically by resolve_sources, so both gaps appear.
        exit_code = main(["--sources", "firms,sentinel", "--regions", "iberia_fire"])
        assert exit_code == 2

    def test_all_regions_have_a_usable_bbox(self) -> None:
        for region in REGIONS:
            assert region.approx_area_km2() > 0
            assert region.bbox_csv().count(",") == 3
