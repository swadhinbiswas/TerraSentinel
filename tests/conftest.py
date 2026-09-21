"""Shared test fixtures: fake Hub uploader, fixed clocks, sample API payloads.

No test in this suite touches the network. HTTP is mocked with ``responses`` at the
``requests`` layer, which means the real retry/backoff/breaker/redaction code paths
are exercised rather than bypassed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# --------------------------------------------------------------------------
# Fake Hugging Face uploader
# --------------------------------------------------------------------------

class FakeUploader:
    """Records what would have been uploaded, and to which path."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.folders: list[tuple[str, str]] = []
        self.commit_messages: list[str] = []
        self.fail_with: Exception | None = None

    def upload_file(self, local_path: str | Path, path_in_repo: str, *, commit_message: str = "") -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        payload = Path(local_path).read_bytes()
        self.files[path_in_repo] = payload
        self.commit_messages.append(commit_message)
        return SimpleNamespace(oid=f"sha{len(self.files):04d}")

    def upload_bytes(
        self, payload: bytes, path_in_repo: str, *, commit_message: str = ""
    ) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        self.files[path_in_repo] = payload
        self.commit_messages.append(commit_message)
        return SimpleNamespace(oid=f"sha{len(self.files):04d}")

    def upload_json(
        self,
        record: Any,
        path_in_repo: str,
        *,
        commit_message: str = "",
    ) -> Any:
        import json as _json

        return self.upload_bytes(
            _json.dumps(record, default=str).encode("utf-8"),
            path_in_repo,
            commit_message=commit_message,
        )

    def upload_folder(
        self,
        local_dir: str | Path,
        path_in_repo: str = "",
        *,
        commit_message: str = "",
        allow_patterns: Any = None,
    ) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        root = Path(local_dir)
        self.folders.append((str(root), path_in_repo))
        self.commit_messages.append(commit_message)
        for path in root.rglob("*.parquet"):
            key = f"{path_in_repo}/{path.relative_to(root)}".replace("//", "/")
            self.files[key] = path.read_bytes()
        return "foldercommit0001"

    @property
    def paths(self) -> list[str]:
        return sorted(self.files)


# --------------------------------------------------------------------------
# Clocks
# --------------------------------------------------------------------------

class StepClock:
    """Deterministic clock: each call returns the next instant, then holds."""

    def __init__(self, *instants: datetime) -> None:
        if not instants:
            raise ValueError("StepClock needs at least one instant")
        self._instants = list(instants)
        self._index = 0

    def __call__(self) -> datetime:
        value = self._instants[min(self._index, len(self._instants) - 1)]
        self._index += 1
        return value


class FakeMonotonic:
    """Manually advanced monotonic clock for breaker timing tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# --------------------------------------------------------------------------
# Sample payloads (trimmed from the real products' shapes)
# --------------------------------------------------------------------------

#: NASA FIRMS VIIRS area-API response (S-NPP style columns).
FIRMS_VIIRS_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight,type\n"
    "39.4521,-8.1234,367.4,0.44,0.48,2024-08-15,1320,N20,VIIRS,n,2.0NRT,298.7,12.5,D,0\n"
    "41.0090,-7.5521,331.1,0.39,0.42,2024-08-15,2312,N20,VIIRS,l,2.0NRT,291.3,3.2,N,0\n"
    "37.8823,-5.0110,410.9,0.51,0.55,2024-08-16,1405,N20,VIIRS,h,2.0NRT,301.2,48.9,D,0\n"
    "38.5001,-9.1122,340.0,0.40,0.44,2024-08-16,930,N20,VIIRS,80,2.0NRT,295.0,9.9,D,2\n"
)

#: MODIS columns differ: brightness instead of bright_ti4, and no `type` band.
FIRMS_MODIS_CSV = (
    "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti4,bright_ti5,frp,daynight\n"
    "40.1234,-6.5432,330.8,1.0,1.0,2024-08-15,1145,Terra,MODIS,72,6.1NRT,321.0,290.1,15.4,D\n"
)

#: FIRMS returns a plain-text message (HTTP 200) for a bad key or exhausted area.
FIRMS_ERROR_TEXT = "Invalid MAP_KEY. Please check the key and try again."

#: NSIDC Sea Ice Index v4.0 daily, including the real citation/units preamble.
NSIDC_DAILY_CSV = (
    "Year, Month, Day,     Extent,    Missing, Source Data\n"
    "YYYY,    MM,  DD, 10^6 sq km, 10^6 sq km, Source data product web sites\n"
    "2024,      9,  13,      4.612,      0.000, ['/ecs/DP1/PM/NSIDC-0051.001/2024.09.13/n_20240913.bin']\n"
    "2024,      9,  14,      4.588,      0.000, ['/ecs/DP1/PM/NSIDC-0051.001/2024.09.14/n_20240914.bin']\n"
    "2024,      9,  15,      4.601,      0.000, ['/ecs/DP1/PM/NSIDC-0051.001/2024.09.15/n_20240915.bin']\n"
    "2024,     10,  01,      4.910,      0.000, ['/ecs/DP1/PM/NSIDC-0051.001/2024.10.01/n_20241001.bin']\n"
)

#: NSIDC 1981-2010 climatology: per-day-of-year baseline with standard deviation.
NSIDC_CLIMATOLOGY_CSV = (
    "std Years = 1981-2010\n"
    "DOY,   Average Extent,   Std Deviation,      10th,      25th,      50th,      75th,      90th\n"
    "001,           13.778,           0.407,    13.183,    13.479,    13.823,    14.095,    14.257\n"
    "002,           13.842,           0.434,    13.201,    13.538,    13.886,    14.164,    14.321\n"
    "258,            4.588,           0.312,     4.101,     4.322,     4.588,     4.811,     5.004\n"
)


@pytest.fixture
def uploader() -> FakeUploader:
    return FakeUploader()


@pytest.fixture
def sleep() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
def tmp_staging(tmp_path: Path) -> Path:
    return tmp_path / "bronze"


def utc(*args: int) -> datetime:
    """Shorthand for building a UTC datetime in tests."""
    return datetime(*args, tzinfo=UTC)
