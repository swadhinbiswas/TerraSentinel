"""Window arithmetic, CF time decoding and the 0/360 longitude seam."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from collectors.time_utils import (
    CF_EPOCH_RE,
    add_days,
    composite_windows,
    decode_cf_time,
    iter_date_windows,
    split_lon_window,
    to_360,
    to_utc,
)


class TestIterDateWindows:
    def test_splits_into_inclusive_slices(self) -> None:
        windows = iter_date_windows(date(2024, 1, 1), date(2024, 1, 10), size_days=5)
        assert windows == [
            (date(2024, 1, 1), date(2024, 1, 5)),
            (date(2024, 1, 6), date(2024, 1, 10)),
        ]

    def test_clips_the_final_window(self) -> None:
        windows = iter_date_windows(date(2024, 1, 1), date(2024, 1, 7), size_days=5)
        assert windows[-1] == (date(2024, 1, 6), date(2024, 1, 7))

    def test_single_day_range(self) -> None:
        assert iter_date_windows(date(2024, 5, 1), date(2024, 5, 1), size_days=5) == [
            (date(2024, 5, 1), date(2024, 5, 1))
        ]

    def test_is_deterministic_for_a_given_range(self) -> None:
        first = iter_date_windows(date(2024, 1, 1), date(2024, 3, 1), size_days=5)
        second = iter_date_windows(date(2024, 1, 1), date(2024, 3, 1), size_days=5)
        assert first == second

    def test_covers_every_day_exactly_once(self) -> None:
        windows = iter_date_windows(date(2024, 1, 1), date(2024, 2, 29), size_days=5)
        covered = [day for start, end in windows for day in pd.date_range(start, end).date]
        assert covered == list(pd.date_range("2024-01-01", "2024-02-29").date)

    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(ValueError, match="after"):
            iter_date_windows(date(2024, 2, 1), date(2024, 1, 1))

    def test_rejects_zero_size(self) -> None:
        with pytest.raises(ValueError, match="size_days"):
            iter_date_windows(date(2024, 1, 1), date(2024, 1, 2), size_days=0)


class TestCompositeWindows:
    def test_weekly_windows_are_seven_days(self) -> None:
        windows = composite_windows(date(2024, 1, 1), date(2024, 1, 20), "W")
        assert windows[0] == (date(2024, 1, 1), date(2024, 1, 7))
        assert windows[1] == (date(2024, 1, 8), date(2024, 1, 14))

    def test_monthly_windows_snap_to_calendar_months(self) -> None:
        windows = composite_windows(date(2024, 1, 15), date(2024, 3, 10), "M")
        assert windows == [
            (date(2024, 1, 15), date(2024, 1, 31)),
            (date(2024, 2, 1), date(2024, 2, 29)),
            (date(2024, 3, 1), date(2024, 3, 10)),
        ]

    def test_monthly_handles_leap_february(self) -> None:
        windows = composite_windows(date(2025, 2, 1), date(2025, 2, 28), "M")
        assert windows == [(date(2025, 2, 1), date(2025, 2, 28))]

    def test_daily_windows(self) -> None:
        windows = composite_windows(date(2024, 1, 1), date(2024, 1, 3), "D")
        assert len(windows) == 3

    def test_unknown_period_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown composite period"):
            composite_windows(date(2024, 1, 1), date(2024, 1, 2), "Q")


class TestDecodeCfTime:
    def test_decodes_oisst_epoch(self) -> None:
        # OISST uses "days since 1800-01-01"; 82545 days in is 2026-01-01.
        decoded = decode_cf_time([82545.0, 82806.0], "days since 1800-01-01 00:00:00")
        assert decoded.iloc[0] == pd.Timestamp("2026-01-01", tz="UTC")
        assert decoded.iloc[1] == pd.Timestamp("2026-09-19", tz="UTC")

    def test_decodes_hours_since_epoch(self) -> None:
        decoded = decode_cf_time([48.0], "hours since 2024-01-01 00:00:00")
        assert decoded.iloc[0] == pd.Timestamp("2024-01-03", tz="UTC")

    def test_result_is_utc_aware(self) -> None:
        assert str(decode_cf_time([0.0], "days since 2000-01-01").dtype) == "datetime64[ns, UTC]"

    def test_rejects_unsupported_units(self) -> None:
        with pytest.raises(ValueError, match="unsupported CF time units"):
            decode_cf_time([1.0], "fortnights since 2000-01-01")

    def test_rejects_empty_units(self) -> None:
        with pytest.raises(ValueError):
            decode_cf_time([1.0], "")

    def test_epoch_pattern_matches_real_attributes(self) -> None:
        match = CF_EPOCH_RE.match("days since 1800-01-01 00:00:00")
        assert match is not None
        assert match.group("unit") == "days"


class TestLongitudeConvention:
    def test_to_360_wraps_negatives(self) -> None:
        assert to_360(-10.0) == 350.0
        assert to_360(19.0) == 19.0
        assert to_360(0.0) == 0.0

    def test_split_across_the_seam(self) -> None:
        # Iberia spans -10..3.5, which on a 0-360 grid is two disjoint windows.
        assert split_lon_window(-10.0, 3.5) == [(350.0, 360.0), (0.0, 3.5)]

    def test_single_window_when_not_crossing(self) -> None:
        assert split_lon_window(19.0, 28.5) == [(19.0, 28.5)]

    def test_east_of_360_becomes_a_single_window(self) -> None:
        assert split_lon_window(350.0, 360.0) == [(350.0, 360.0)]

    def test_unwrapped_grid_is_unchanged(self) -> None:
        assert split_lon_window(-10.0, 3.5, grid="-180-180") == [(-10.0, 3.5)]

    def test_full_globe(self) -> None:
        assert split_lon_window(-180.0, 180.0) == [(0.0, 360.0)]


class TestMisc:
    def test_add_days(self) -> None:
        assert add_days(date(2024, 2, 28), 2) == date(2024, 3, 1)

    def test_to_utc_localises_naive_input(self) -> None:
        assert to_utc("2024-01-01T00:00:00").tzinfo is not None
        assert to_utc(date(2024, 1, 1)).hour == 0
