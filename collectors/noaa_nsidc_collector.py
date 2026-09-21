"""NOAA + NSIDC collector: sea-surface temperature anomalies and sea-ice extent.

Three arms, because the "ocean/ice" signal lives in three different places:

1. **NOAA OISST v2.1 daily SST anomaly** (PSL THREDDS, OPeNDAP) — a gridded field
   in 0..360 longitude whose time axis is CF-encoded as *days since 1800-01-01*.
   Marine heatwaves in the Mediterranean and Norwegian Sea are real precursors
   for both fire risk and glacier melt, so this feeds the fire and ice models.
2. **NSIDC Sea Ice Index v4.0 daily extent** — one hemispheric number per day per
   pole. The whole ~1.9 MB series is downloaded and filtered to the window, which
   is cheap and means a re-run always sees the full corrected history rather than
   only what was on the wire the first time.
3. **NSIDC 1981-2010 climatology** — a static per-day-of-year baseline with a
   standard deviation. This is the seasonal reference the anomaly models divide
   by; "same week last year" is a worse baseline than a 30-year normal.

Note that arm 3 is only fetched for backfills or when explicitly requested: it is
immutable, so re-downloading it weekly would just rewrite identical files.
"""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from collectors.base_collector import BaseCollector, main_for
from collectors.config import Region
from collectors.time_utils import decode_cf_time, split_lon_window
from ops.resilience import PermanentError, TransientError
from pandera_schemas import BRONZE_NOAA_NSIDC

LOGGER = logging.getLogger(__name__)

#: NSIDC Sea Ice Index v4.0 daily extent, and the frozen 1981-2010 baseline.
NSIDC_DAILY_URLS: dict[str, str] = {
    "arctic": "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/N_seaice_extent_daily_v4.0.csv",
    "antarctic": "https://noaadata.apps.nsidc.org/NOAA/G02135/south/daily/data/S_seaice_extent_daily_v4.0.csv",
}
NSIDC_CLIMATOLOGY_URLS: dict[str, str] = {
    "arctic": (
        "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/"
        "N_seaice_extent_climatology_1981-2010_v4.0.csv"
    ),
    "antarctic": (
        "https://noaadata.apps.nsidc.org/NOAA/G02135/south/daily/data/"
        "S_seaice_extent_climatology_1981-2010_v4.0.csv"
    ),
}

NSIDC_HEMISPHERES = tuple(NSIDC_DAILY_URLS)

#: Non-leap reference year: makes day-of-year map cleanly onto (month, day) so the
#: climatology baseline can be joined on either key downstream.
CLIMATOLOGY_REFERENCE_YEAR = 2001

OISST_OPENDAP_BASE = "https://psl.noaa.gov/thredds/dodsC/Datasets/noaa.oisst.v2.highres"
OISST_PRODUCT = "noaa_oisst_v2_1_anom"
NSIDC_PRODUCT = "nsidc_seaice_index_v4_0"

#: OISST is a 0.25 degree grid; sample every 1 degree for regional summaries.
OISST_GRID_STEP_DEG = 0.25
OISST_SAMPLE_DEG = 1.0
OISST_LON_GRID = "0-360"


def read_csv_after_header(text: str, sentinel: str, *, units_rows: int = 0) -> pd.DataFrame:
    """Read a CSV whose real header is preceded by citation/units lines.

    Both NSIDC products begin with metadata lines (``std Years = 1981-2010``, a
    units row, a citation row) whose field counts differ from the data, so
    ``skiprows`` has to be discovered rather than hard-coded. ``units_rows``
    additionally skips the units line that the daily series places directly
    under its header.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower().startswith(sentinel.lower()):
            header = lines[index]
            data = lines[index + 1 + units_rows :]
            try:
                return pd.read_csv(io.StringIO("\n".join([header, *data])), skipinitialspace=True)
            except pd.errors.ParserError as exc:
                raise PermanentError(
                    f"could not parse CSV starting at line {index} (sentinel {sentinel!r}): {exc}"
                ) from exc
    raise PermanentError(
        f"could not find a header line starting with {sentinel!r} in the first "
        f"{len(lines)} line(s) of the payload"
    )


class NoaaNsidcCollector(BaseCollector):
    """NOAA OISST SST anomalies + NSIDC sea-ice extent and climatology."""

    source_id = "noaa_nsidc"
    metric_type = "sea_ice_extent"
    label = "NOAA OISST + NSIDC Sea Ice Index"
    raw_schema = BRONZE_NOAA_NSIDC
    time_column = "timestamp"
    default_region_ids = ("arctic", "antarctic", "norway_ice", "iberia_fire", "greece_fire")

    live_lookback_days = 14

    def __init__(
        self,
        *,
        include_climatology: bool = False,
        sample_deg: float = OISST_SAMPLE_DEG,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.include_climatology = include_climatology
        self.sample_deg = sample_deg

    # -- dispatch ----------------------------------------------------------

    def fetch(
        self,
        region: Region,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        backfill: bool = False,
        **_kwargs: Any,
    ) -> pd.DataFrame:
        end = end_date or self.now().date()
        start = start_date or (end - timedelta(days=self.live_lookback_days))
        if start > end:
            raise PermanentError(f"start_date {start} is after end_date {end}")

        if region.region_id in NSIDC_HEMISPHERES:
            frames = [self._fetch_sea_ice(region, start, end)]
            if backfill or self.include_climatology:
                frames.append(self._fetch_climatology(region))
            usable = [frame for frame in frames if not frame.empty]
            if not usable:
                return pd.DataFrame()
            return pd.concat(usable, ignore_index=True)

        return self._fetch_sst_anomaly(region, start, end)

    # -- NSIDC arm ---------------------------------------------------------

    def _fetch_sea_ice(self, region: Region, start: date, end: date) -> pd.DataFrame:
        url = NSIDC_DAILY_URLS[region.region_id]
        text = self.get_text(url)
        raw = read_csv_after_header(text, sentinel="Year", units_rows=1)

        raw.columns = [str(column).strip().lower() for column in raw.columns]
        required = {"year", "month", "day", "extent"}
        missing = required - set(raw.columns)
        if missing:
            raise PermanentError(
                f"NSIDC payload is missing column(s) {sorted(missing)}; received {list(raw.columns)}"
            )

        for column in ("year", "month", "day", "extent"):
            raw[column] = pd.to_numeric(raw[column], errors="coerce")
        raw = raw.dropna(subset=["year", "month", "day", "extent"])

        stamps = pd.to_datetime(
            {
                "year": raw["year"].astype(int),
                "month": raw["month"].astype(int),
                "day": raw["day"].astype(int),
            },
            utc=True,
            errors="coerce",
        )
        raw = raw.assign(timestamp=stamps).dropna(subset=["timestamp"])
        raw = raw[(raw["timestamp"] >= pd.Timestamp(start, tz="UTC")) & (raw["timestamp"] <= pd.Timestamp(end, tz="UTC"))]

        if raw.empty:
            self.log(
                logging.WARNING,
                f"nsidc/{region.region_id}: no rows between {start} and {end}",
                region_id=region.region_id,
            )
            return pd.DataFrame()

        centre_lat, centre_lon = _bbox_center(region)
        return pd.DataFrame(
            {
                "timestamp": raw["timestamp"],
                "latitude": centre_lat,
                "longitude": centre_lon,
                "metric_type": "sea_ice_extent",
                "product": NSIDC_PRODUCT,
                "value": raw["extent"].astype("float64"),
                "unit": "10^6 km^2",
                "spatial_scope": "hemispheric",
                "region_id": region.region_id,
                "source_dataset": url.rsplit("/", 1)[-1],
            }
        ).reset_index(drop=True)

    def _fetch_climatology(self, region: Region) -> pd.DataFrame:
        url = NSIDC_CLIMATOLOGY_URLS[region.region_id]
        text = self.get_text(url)
        raw = read_csv_after_header(text, sentinel="DOY")

        raw.columns = [str(column).strip().lower() for column in raw.columns]
        by_name = {column: column for column in raw.columns}
        value_column = next(
            (name for name in ("average extent", "extent") if name in by_name), None
        )
        std_column = next((name for name in ("std deviation", "stddev") if name in by_name), None)
        if "doy" not in raw.columns or value_column is None:
            raise PermanentError(
                f"NSIDC climatology payload has unexpected columns {list(raw.columns)}"
            )

        doy = pd.to_numeric(raw["doy"], errors="coerce")
        value = pd.to_numeric(raw[value_column], errors="coerce")
        stddev = pd.to_numeric(raw[std_column], errors="coerce") if std_column else np.nan

        frame = pd.DataFrame(
            {
                "day_of_year": doy,
                "value": value,
                "baseline_stddev": stddev,
            }
        ).dropna(subset=["day_of_year", "value"])

        # Anchor each row to a non-leap reference year so a day-of-year is
        # unambiguous and the frame still flows through the normal partitioner.
        # Day 366 only exists in leap years and means "the last day of the year",
        # so it clamps to Dec 31 rather than spilling into the next January —
        # `day_of_year` is what downstream joins on, the timestamp is provenance.
        reference = pd.Timestamp(f"{CLIMATOLOGY_REFERENCE_YEAR}-01-01", tz="UTC")
        day_offset = frame["day_of_year"].clip(upper=365) - 1
        frame["timestamp"] = reference + pd.to_timedelta(day_offset, unit="D")

        centre_lat, centre_lon = _bbox_center(region)
        frame["latitude"] = centre_lat
        frame["longitude"] = centre_lon
        frame["metric_type"] = "sea_ice_extent_climatology"
        frame["product"] = NSIDC_PRODUCT
        frame["unit"] = "10^6 km^2"
        frame["spatial_scope"] = "hemispheric"
        frame["region_id"] = region.region_id
        frame["source_dataset"] = url.rsplit("/", 1)[-1]
        return frame.reset_index(drop=True)

    # -- NOAA arm ----------------------------------------------------------

    def _fetch_sst_anomaly(self, region: Region, start: date, end: date) -> pd.DataFrame:
        open_url = _import_pydap()
        west, south, east, north = region.bbox
        lon_windows = split_lon_window(west, east, grid=OISST_LON_GRID)

        frames: list[pd.DataFrame] = []
        for year in range(start.year, end.year + 1):
            url = f"{OISST_OPENDAP_BASE}/sst.day.anom.{year}.nc"
            try:
                dataset = open_url(url)
            except Exception as exc:  # noqa: BLE001 - any failure here is server/network
                raise TransientError(f"could not open OPeNDAP dataset {url}: {exc}") from exc

            times = decode_cf_time(dataset["time"][:].data, str(dataset["time"].attributes.get("units", "")))
            lats = np.asarray(dataset["lat"][:].data, dtype="float64")
            lons = np.asarray(dataset["lon"][:].data, dtype="float64")
            variable = dataset["anom"]
            missing = _missing_value(variable.attributes)
            rank = len(variable.shape)

            in_window = (times >= pd.Timestamp(start, tz="UTC")) & (
                times <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
            )
            time_index = np.flatnonzero(in_window.to_numpy())
            lat_index = np.flatnonzero((lats >= south) & (lats <= north))
            if time_index.size == 0 or lat_index.size == 0:
                continue

            for lon_low, lon_high in lon_windows:
                lon_index = np.flatnonzero((lons >= lon_low) & (lons <= lon_high))
                if lon_index.size == 0:
                    continue

                span = (
                    int(time_index[0]),
                    int(time_index[-1]) + 1,
                    int(lat_index[0]),
                    int(lat_index[-1]) + 1,
                    int(lon_index[0]),
                    int(lon_index[-1]) + 1,
                )
                frames.append(
                    self._block_to_frame(
                        self._read_block(variable, span, rank=rank),
                        times.iloc[time_index].to_numpy(),
                        lats[lat_index],
                        lons[lon_index],
                        missing=missing,
                        region_id=region.region_id,
                        source_dataset=url.rsplit("/", 1)[-1],
                    )
                )

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _read_block(self, variable: Any, span: tuple[int, ...], *, rank: int) -> np.ndarray:
        """Read a (time, lat, lon) block, tolerating how the server exposes axes.

        The same OISST variable is 3-D ``(time, lat, lon)`` on one THREDDS
        endpoint and 4-D with a singleton depth axis on another. Getting this wrong
        does not raise on its own — it silently misaligns the axes and produces
        plausible nonsense — so the element count is checked before reshaping and
        a mismatch is a hard failure.
        """
        start_t, stop_t, start_lat, stop_lat, start_lon, stop_lon = span
        if rank == 3:
            selection = (
                slice(start_t, stop_t),
                slice(start_lat, stop_lat),
                slice(start_lon, stop_lon),
            )
        elif rank == 4:
            selection = (
                slice(start_t, stop_t),
                slice(0, 1),
                slice(start_lat, stop_lat),
                slice(start_lon, stop_lon),
            )
        else:
            raise PermanentError(
                f"OISST variable has {rank} dimensions; expected 3 (time, lat, lon) or 4 "
                "with a singleton depth axis"
            )

        expected = (stop_t - start_t, stop_lat - start_lat, stop_lon - start_lon)
        raw = np.asarray(variable[selection].data, dtype="float64")
        if raw.size != expected[0] * expected[1] * expected[2]:
            raise PermanentError(
                f"OISST block read returned {raw.size} value(s), expected {expected}. "
                "The upstream axis layout changed — refusing to guess at the alignment."
            )
        return raw.reshape(expected)

    def _block_to_frame(
        self,
        values: np.ndarray,
        times: np.ndarray,
        lats: np.ndarray,
        lons: np.ndarray,
        *,
        missing: float,
        region_id: str,
        source_dataset: str,
    ) -> pd.DataFrame:
        """Flatten a (time, lat, lon) block into long rows, subsampling spatially."""
        lat_step, lon_step = _stride_for(self.sample_deg), _stride_for(self.sample_deg)
        lats = lats[::lat_step]
        lons = lons[::lon_step]
        values = values[:, ::lat_step, ::lon_step]

        time_count, lat_count, lon_count = values.shape
        flat = values.reshape(time_count, lat_count * lon_count)
        valid = np.isfinite(flat) & ~np.isclose(flat, missing, rtol=1e-6)
        time_pos, cell = np.nonzero(valid)
        lat_pos, lon_pos = np.divmod(cell, lon_count)

        return pd.DataFrame(
            {
                "timestamp": times[time_pos],
                "latitude": lats[lat_pos],
                # Publish back in -180..180 so every layer agrees on one convention.
                "longitude": ((lons[lon_pos] + 180.0) % 360.0) - 180.0,
                "metric_type": "sst_anomaly",
                "product": OISST_PRODUCT,
                "value": flat[time_pos, cell],
                "unit": "degC",
                "spatial_scope": "gridded",
                "region_id": region_id,
                "source_dataset": source_dataset,
            }
        )


def _import_pydap() -> Any:
    try:
        from pydap.client import open_url
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise PermanentError(
            "the NOAA SST arm needs pydap: install with `uv pip install -e '.[noaa]'`"
        ) from exc
    return open_url


def _missing_value(attributes: dict[str, Any]) -> float:
    for key in ("missing_value", "_FillValue"):
        if key in attributes:
            try:
                return float(attributes[key])
            except (TypeError, ValueError):
                continue
    # Physical guard: SST anomalies never approach this magnitude.
    return -9.96921e36


def _stride_for(sample_deg: float) -> int:
    if sample_deg <= 0:
        raise ValueError(f"sample_deg must be > 0, got {sample_deg}")
    return max(1, int(round(sample_deg / OISST_GRID_STEP_DEG)))


def _bbox_center(region: Region) -> tuple[float, float]:
    west, south, east, north = region.bbox
    return (south + north) / 2.0, (west + east) / 2.0


if __name__ == "__main__":
    raise SystemExit(main_for(NoaaNsidcCollector))
