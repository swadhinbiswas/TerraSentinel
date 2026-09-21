"""Date/window arithmetic and CF time decoding shared by the collectors.

Three jobs, all pure and unit-testable:

* **Windowing** — FIRMS' area API accepts at most 5 days per request, so a
  backfill has to walk a date range in fixed slices.
* **CF time decoding** — OPeNDAP/netCDF time axes are "``<unit> since <epoch>``"
  (OISST uses ``days since 1800-01-01``), not Unix epoch. Reading the units
  attribute and converting is the difference between correct dates and silently
  offsetting everything by 170 years.
* **Longitude convention** — gridded NOAA products are published in 0..360 while
  our regions, H3 and dashboards are all -180..180. A bbox that straddles the
  prime meridian becomes two windows.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pandas as pd

__all__ = [
    "CF_EPOCH_RE",
    "add_days",
    "composite_windows",
    "decode_cf_time",
    "iter_date_windows",
    "split_lon_window",
    "to_360",
    "to_utc",
    "utc_now",
]

CF_EPOCH_RE = re.compile(
    r"^\s*(?P<unit>days|hours|minutes|seconds)\s+since\s+(?P<epoch>.+?)\s*$",
    re.IGNORECASE,
)

_UNIT_TO_TIMEDELTA = {
    "days": lambda n: pd.to_timedelta(n, unit="D"),
    "hours": lambda n: pd.to_timedelta(n, unit="h"),
    "minutes": lambda n: pd.to_timedelta(n, unit="m"),
    "seconds": lambda n: pd.to_timedelta(n, unit="s"),
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime | date | str) -> datetime:
    """Coerce a date/datetime/ISO string to an aware UTC datetime."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").to_pydatetime()


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)


def iter_date_windows(
    start: date,
    end: date,
    *,
    size_days: int = 5,
) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into inclusive windows of at most ``size_days``.

    Windows are anchored on ``start`` rather than on calendar boundaries so the
    result is deterministic: the same range always produces the same requests,
    which is what makes a re-run land on identical bronze paths.
    """
    if size_days < 1:
        raise ValueError(f"size_days must be >= 1, got {size_days}")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=size_days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def composite_windows(start: date, end: date, period: str) -> list[tuple[date, date]]:
    """Date windows for satellite composites: ``"D"``, ``"W"`` or ``"M"``.

    ``"W"`` uses anchored 7-day slices (up to 5 days of revisit coverage is ample
    for Sentinel-1/2), while ``"M"`` snaps to calendar months so a monthly NDVI
    series lines up with climatology and with the NSIDC monthly baseline. The
    first and last windows are clipped to the requested range.
    """
    key = period.strip().upper()
    if key == "D":
        return iter_date_windows(start, end, size_days=1)
    if key == "W":
        return iter_date_windows(start, end, size_days=7)
    if key == "M":
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        windows: list[tuple[date, date]] = []
        for month in pd.period_range(start, end, freq="M"):
            window_start = max(month.start_time.date(), start)
            window_end = min(month.end_time.date(), end)
            windows.append((window_start, window_end))
        return windows
    raise ValueError(f"unknown composite period {period!r}; expected 'D', 'W' or 'M'")


def decode_cf_time(values: object, units: str) -> pd.Series:
    """Convert a CF-encoded time axis to a UTC ``pandas`` datetime Series.

    ``units`` must look like ``"days since 1800-01-01 00:00:00"``. Anything else
    raises rather than guessing, because a wrong epoch produces plausible-looking
    dates that silently poison every downstream join.
    """
    match = CF_EPOCH_RE.match(units or "")
    if not match:
        raise ValueError(f"unsupported CF time units {units!r}; expected '<unit> since <epoch>'")

    unit = match.group("unit").lower()
    epoch_raw = match.group("epoch").strip()
    epoch = pd.Timestamp(epoch_raw)
    if epoch.tzinfo is None:
        epoch = epoch.tz_localize("UTC")

    series = pd.Series(values).astype("float64")
    offsets = _UNIT_TO_TIMEDELTA[unit](series)
    return epoch + offsets


def to_360(lon: float) -> float:
    """Map a longitude to the 0..360 convention used by gridded NOAA products."""
    return float(lon) % 360.0


def split_lon_window(west: float, east: float, *, grid: str = "0-360") -> list[tuple[float, float]]:
    """Longitude sub-windows for a bbox, accounting for the 0/360 seam.

    With ``grid="0-360"`` a bbox of ``(-10, 3.5)`` becomes ``[(350, 360), (0, 3.5)]``
    — two requests, because the interval wraps the prime meridian instead of
    crossing it. With ``grid="-180-180"`` the bbox is returned unchanged.
    """
    if grid != "0-360":
        return [(float(west), float(east))]
    if east - west >= 360.0:
        return [(0.0, 360.0)]

    low = to_360(west)
    high = to_360(east)
    if high == 0.0 and east > 0.0:
        high = 360.0

    if low < high:
        return [(low, high)]
    if low == high:
        return [(low, low)]
    return [(low, 360.0), (0.0, high)]
