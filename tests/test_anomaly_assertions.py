"""The Phase 5 injection check must be falsifiable.

A checker that always passes is worse than no checker, so these tests construct the
two degenerate worlds it has to reject: one where nothing is ever anomalous, and one
where a broken baseline flags everything.
"""

from __future__ import annotations

import duckdb

from tools.assert_anomalies_detected import MAX_FIRE_FLAG_RATE, run_checks


def build_gold(
    *,
    fire_days: int = 100,
    spike_day: bool = True,
    flag_everything: bool = False,
    ndvi_crash: bool = True,
    ice_excursion: bool = True,
) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute("create schema gold")

    fire_rows = []
    for day in range(fire_days):
        is_spike = spike_day and day == 0
        # A healthy baseline flags only the spike; a degenerate one flags every day.
        fire_rows.append(
            (
                day,
                60 if is_spike else 3,
                20.0 if is_spike else 0.2,
                True if flag_everything else is_spike,
            )
        )

    connection.execute(
        "create table gold.gold_fire_anomalies ("
        "  observation_date bigint, detection_count bigint, zscore double,"
        "  is_anomaly boolean, region_id varchar"
        ")"
    )
    connection.executemany(
        "insert into gold.gold_fire_anomalies values (?, ?, ?, ?, 'iberia_fire')",
        fire_rows,
    )

    connection.execute(
        "create table gold.gold_deforestation_index as "
        "select 'carpathian_deforest' as region_id, "
        f"{'-0.42' if ndvi_crash else '-0.02'} as ndvi_change, "
        f"{'true' if ndvi_crash else 'false'} as is_anomaly"
    )
    connection.execute(
        "create table gold.gold_ice_extent_trends as "
        "select 'arctic' as region_id, "
        f"{'-4.31' if ice_excursion else '-0.5'} as zscore, "
        f"{'true' if ice_excursion else 'false'} as is_anomaly"
    )
    connection.execute(
        "create table gold.gold_h3_sst as "
        "select 'noaa_nsidc' as source_id, 'sst_anomaly' as metric_type, 3.4 as value"
    )
    connection.execute(
        "create table gold.gold_h3_fire as "
        "select 'firms' as source_id, 'fire_detection_count' as metric_type, 12.0 as value"
    )
    return connection


def check(checks, name: str):
    return next(item for item in checks if item.name == name)


class TestHealthyWorld:
    def test_all_checks_pass(self) -> None:
        checks = run_checks(build_gold())
        assert all(item.passed for item in checks), [item.as_dict() for item in checks if not item.passed]


class TestDegenerateWorlds:
    def test_missing_injected_events_fail_sensitivity(self) -> None:
        checks = run_checks(build_gold(spike_day=False, ndvi_crash=False, ice_excursion=False))
        assert not check(checks, "fire_spike_detected").passed
        assert not check(checks, "ndvi_crash_detected").passed
        assert not check(checks, "ice_excursion_detected").passed

    def test_flagging_everything_fails_specificity(self) -> None:
        # A baseline with a zero scale would flag every day, which would sail past
        # the sensitivity checks while being useless.
        checks = run_checks(build_gold(flag_everything=True))
        assert check(checks, "fire_spike_detected").passed
        assert not check(checks, "fire_flag_rate_is_selective").passed

    def test_empty_marts_fail(self) -> None:
        connection = build_gold()
        connection.execute("delete from gold.gold_h3_sst")
        checks = run_checks(connection)
        assert not check(checks, "gold_marts_are_populated").passed

    def test_flag_rate_ceiling_is_strict(self) -> None:
        assert 0 < MAX_FIRE_FLAG_RATE < 0.1
