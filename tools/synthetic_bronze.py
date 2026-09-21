"""Generate a synthetic bronze lake for local development and CI.

The transform and ML layers have to be developable and testable without
credentials, and "the tests pass" must not depend on a live upstream API being up.
This writes the *same* hive-partitioned layout the collectors write — including the
`backfill/` prefix for the historical portion and daily partitions for the recent
tail — so the dbt models read local data and Hub data through identical code paths.

Generated frames are validated against the collectors' own Pandera schemas before
being written. That makes this a contract test as much as a fixture: if the bronze
contract and the generator ever disagree, this fails loudly rather than producing a
lake that silently does not match production.

`--inject-anomaly` plants a single grossly obvious event (a fire spike, an NDVI
crash, an ice-extent excursion). Phase 5's synthetic-injection test uses that same
switch to assert the pipeline still catches something it cannot miss.

Usage::

    python -m tools.synthetic_bronze --out data/bronze
    python -m tools.synthetic_bronze --out data/bronze --inject-anomaly
"""

from __future__ import annotations

import argparse
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import h3
import pandas as pd

from collectors.config import REGIONS, Region, bronze_filename, bronze_prefix, get_region
from collectors.geo_utils import cell_center, cells_for_bbox
from pandera_schemas import (
    BRONZE_ENVELOPE,
    BRONZE_FIRMS,
    BRONZE_NOAA_NSIDC,
    BRONZE_SENTINEL,
    conform_to_schema,
    validate_frame,
)

FIRMS_PRODUCTS = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT")
FIRE_REGIONS = ("iberia_fire", "greece_fire")
SENTINEL_REGIONS = ("carpathian_deforest", "alps_ice", "norway_ice")
MARINE_REGIONS = ("iberia_fire", "greece_fire", "norway_ice")
HEMISPHERES = ("arctic", "antarctic")

NDVI_SEASONAL_MEAN = 0.68
NDVI_SEASONAL_AMPLITUDE = 0.18
NDVI_ANNUAL_DECLINE = 0.045
ARCTIC_MEAN_EXTENT = 12.0
ANTARCTIC_MEAN_EXTENT = 11.0
ARCTIC_SEASONAL_AMPLITUDE = 4.2
SST_ANOMALY_NOISE = 0.35


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _seasonal_factor(day: date, *, peak_doy: int, amplitude: float) -> float:
    """Cosine seasonal shape peaking at ``peak_doy`` (day-of-year)."""
    doy = day.timetuple().tm_yday
    return amplitude * math.cos(2.0 * math.pi * (doy - peak_doy) / 365.25)


def _sea_ice_mean(day: date, *, north: bool) -> float:
    """Seasonal sea-ice extent for a hemisphere.

    One definition used by both the daily series and the climatology, because a
    baseline generated from a different formula than the series it baselines
    produces nonsense z-scores — Arctic peaks in March, Antarctic in September.
    """
    if north:
        return ARCTIC_MEAN_EXTENT + _seasonal_factor(
            day, peak_doy=75, amplitude=ARCTIC_SEASONAL_AMPLITUDE
        )
    return ANTARCTIC_MEAN_EXTENT + _seasonal_factor(
        day, peak_doy=260, amplitude=ARCTIC_SEASONAL_AMPLITUDE
    )


def _random_point(region: Region, rng: random.Random) -> tuple[float, float]:
    west, south, east, north = region.bbox
    return rng.uniform(south, north), rng.uniform(west, east)


def _envelope(
    frame: pd.DataFrame,
    *,
    source_id: str,
    metric_type: str,
    region_id: str,
    resolution: int,
    ingested_at: datetime,
    run_id: str,
) -> pd.DataFrame:
    """Add the provenance columns every bronze landing carries."""
    out = frame.copy()
    out["source_id"] = source_id
    out["metric_type"] = metric_type
    out["region_id"] = region_id
    out["ingested_at"] = ingested_at
    out["run_id"] = run_id
    if "h3_index" not in out.columns:
        out["h3_index"] = [
            h3.latlng_to_cell(lat, lon, resolution)
            for lat, lon in zip(out["latitude"], out["longitude"], strict=True)
        ]
    return out


# --------------------------------------------------------------------------
# FIRMS
# --------------------------------------------------------------------------

def generate_firms(
    out_root: Path,
    *,
    regions: tuple[str, ...],
    start: date,
    end: date,
    rng: random.Random,
    ingested_at: datetime,
    run_id: str,
    inject_anomaly: bool,
) -> int:
    """Daily fire detections, with a Mediterranean summer peak."""
    rows_written = 0
    anomaly_date = date(end.year, 8, 15)

    for region_id in regions:
        region = get_region(region_id)
        day = start
        while day <= end:
            # Summer peak (Jul), near-zero in winter, plus a small background.
            expected = max(0.0, 1.4 + _seasonal_factor(day, peak_doy=200, amplitude=5.0))
            if inject_anomaly and region_id == FIRE_REGIONS[0] and day == anomaly_date:
                expected = 60.0

            count = max(0, int(rng.gauss(expected, math.sqrt(max(expected, 0.5)))))
            if count:
                detections = []
                for _ in range(count):
                    lat, lon = _random_point(region, rng)
                    hour = rng.randrange(24)
                    minute = rng.randrange(60)
                    acquisition = datetime(day.year, day.month, day.day, hour, minute)
                    confidence_pct = min(100.0, max(0.0, rng.gauss(78.0, 14.0)))
                    product = rng.choice(FIRMS_PRODUCTS)
                    detections.append(
                        {
                            "latitude": lat,
                            "longitude": lon,
                            "acq_datetime": acquisition,
                            "acq_date": day.isoformat(),
                            "acq_time": f"{hour:02d}{minute:02d}",
                            "product": product,
                            "confidence": (
                                "low"
                                if confidence_pct < 30
                                else ("nominal" if confidence_pct < 80 else "high")
                            ),
                            "confidence_pct": confidence_pct,
                            "satellite": "N20",
                            "instrument": "VIIRS",
                            "daynight": "D" if 6 <= hour <= 18 else "N",
                            "frp": max(0.1, rng.lognormvariate(1.4, 0.8)),
                            # MODIS reports band-21 brightness; VIIRS reports
                            # bright_ti4. Mirroring that here is what proves the
                            # staging coalesce copes with a lake holding one product.
                            "bright_ti4": None if product == "MODIS_NRT" else rng.gauss(340.0, 18.0),
                            "bright_ti5": None if product == "MODIS_NRT" else rng.gauss(295.0, 6.0),
                            "brightness": rng.gauss(330.0, 15.0) if product == "MODIS_NRT" else None,
                            "scan": 0.44,
                            "track": 0.48,
                            "version": "2.0NRT",
                            "type": 0.0,
                        }
                    )

                frame = pd.DataFrame(detections)
                frame["acq_datetime"] = pd.to_datetime(frame["acq_datetime"], utc=True)
                frame = _envelope(
                    frame,
                    source_id="firms",
                    metric_type="fire_radiative_power",
                    region_id=region_id,
                    resolution=7,
                    ingested_at=ingested_at,
                    run_id=run_id,
                )
                frame = validate_frame(conform_to_schema(frame, BRONZE_FIRMS), BRONZE_FIRMS)
                frame = validate_frame(frame, BRONZE_ENVELOPE)

                # Mirror production: everything older than the live tail lands as
                # monthly backfill partitions, the last few days as daily ones.
                _write_partitioned(
                    frame,
                    out_root,
                    "firms",
                    region_id,
                    time_column="acq_datetime",
                    backfill=day <= end - timedelta(days=2),
                )
                rows_written += count
            day += timedelta(days=1)
    return rows_written


# --------------------------------------------------------------------------
# Sentinel
# --------------------------------------------------------------------------

def generate_sentinel(
    out_root: Path,
    *,
    regions: tuple[str, ...],
    start: date,
    end: date,
    rng: random.Random,
    ingested_at: datetime,
    run_id: str,
    inject_anomaly: bool,
) -> int:
    """Monthly region-grain composites plus a res-5 change map per region."""
    rows_written = 0
    months = pd.period_range(start, end, freq="M")
    anomaly_month = pd.Period(f"{end.year}-08", freq="M")

    for region_id in regions:
        region = get_region(region_id)
        rows = []
        for month in months:
            if region.anomaly_type == "deforestation":
                # Green in summer, plus a slow logging decline over the period.
                years_elapsed = (month.start_time.date() - start).days / 365.25
                mean = (
                    NDVI_SEASONAL_MEAN
                    + _seasonal_factor(month.start_time.date(), peak_doy=200, amplitude=NDVI_SEASONAL_AMPLITUDE)
                    - NDVI_ANNUAL_DECLINE * years_elapsed
                )
                if inject_anomaly and region_id == "carpathian_deforest" and month == anomaly_month:
                    mean -= 0.35  # an unmistakable clearance event
                product, metric = "S2_SR_HARMONIZED", "ndvi"
                cloud_pct = rng.uniform(5.0, 45.0)
            else:
                # SAR backscatter in dB: stable, slightly higher in winter.
                mean = -9.5 + _seasonal_factor(month.start_time.date(), peak_doy=15, amplitude=0.8)
                product, metric = "S1_GRD", "sar_backscatter"
                cloud_pct = None

            noise = rng.gauss(0.0, 0.02 if metric == "ndvi" else 0.15)
            value = mean + noise
            rows.append(
                {
                    "window_start": pd.Timestamp(month.start_time.date(), tz="UTC"),
                    "window_end": pd.Timestamp(month.end_time.date(), tz="UTC"),
                    "latitude": (region.bbox[1] + region.bbox[3]) / 2.0,
                    "longitude": (region.bbox[0] + region.bbox[2]) / 2.0,
                    "product": product,
                    "metric_type": metric,
                    "mean": value,
                    "stddev": abs(rng.gauss(0.08, 0.02)),
                    "min": value - 0.15,
                    "max": value + 0.15,
                    "observation_count": rng.randint(400, 4000),
                    "cloud_pct": cloud_pct,
                    "spatial_scope": "region",
                }
            )

        frame = pd.DataFrame(rows)
        frame = _envelope(
            frame,
            source_id="sentinel",
            metric_type="ndvi" if region.anomaly_type == "deforestation" else "sar_backscatter",
            region_id=region_id,
            resolution=region.h3_resolution,
            ingested_at=ingested_at,
            run_id=run_id,
        )
        frame = validate_frame(conform_to_schema(frame, BRONZE_SENTINEL), BRONZE_SENTINEL)
        frame = validate_frame(frame, BRONZE_ENVELOPE)
        _write_partitioned(
            frame, out_root, "sentinel", region_id, time_column="window_end", backfill=True
        )

        # Grid grain: year-over-year change per res-5 cell.
        cells = cells_for_bbox(region.bbox, 5)
        grid_rows = []
        for cell in cells:
            cell_lat, cell_lon = cell_center(cell)
            baseline = rng.gauss(-9.3 if region.anomaly_type != "deforestation" else 0.72, 0.05)
            change = rng.gauss(0.0, 0.03)
            grid_rows.append(
                {
                    "window_start": pd.Timestamp(end - timedelta(days=30), tz="UTC"),
                    "window_end": pd.Timestamp(end, tz="UTC"),
                    "latitude": cell_lat,
                    "longitude": cell_lon,
                    "h3_index": cell,
                    "product": "S2_SR_HARMONIZED" if region.anomaly_type == "deforestation" else "S1_GRD",
                    "metric_type": "ndvi" if region.anomaly_type == "deforestation" else "sar_backscatter",
                    "mean": change,
                    "baseline_mean": baseline,
                    "recent_mean": baseline + change,
                    "spatial_scope": "grid",
                }
            )
        grid = pd.DataFrame(grid_rows)
        grid = _envelope(
            grid,
            source_id="sentinel",
            metric_type="ndvi" if region.anomaly_type == "deforestation" else "sar_backscatter",
            region_id=region_id,
            resolution=5,
            ingested_at=ingested_at,
            run_id=run_id,
        )
        grid = validate_frame(conform_to_schema(grid, BRONZE_SENTINEL), BRONZE_SENTINEL)
        grid = validate_frame(grid, BRONZE_ENVELOPE)
        _write_partitioned(
            grid, out_root, "sentinel", region_id, time_column="window_end", backfill=False
        )
        rows_written += len(frame) + len(grid)
    return rows_written


# --------------------------------------------------------------------------
# NOAA / NSIDC
# --------------------------------------------------------------------------

def generate_noaa_nsidc(
    out_root: Path,
    *,
    regions: tuple[str, ...],
    start: date,
    end: date,
    rng: random.Random,
    ingested_at: datetime,
    run_id: str,
    inject_anomaly: bool,
) -> int:
    """Daily hemispheric sea-ice extent, the 1981-2010 climatology, and monthly SST anomalies."""
    rows_written = 0

    for region_id in [value for value in regions if value in HEMISPHERES]:
        north = region_id == "arctic"
        region = get_region(region_id)
        lat = (region.bbox[1] + region.bbox[3]) / 2.0
        lon = 0.0

        rows = []
        day = start
        while day <= end:
            value = _sea_ice_mean(day, north=north) + rng.gauss(0.0, 0.06)
            if inject_anomaly and north and day == date(end.year, 9, 10):
                value -= 1.8  # a record-low excursion
            rows.append(
                {
                    "timestamp": pd.Timestamp(day, tz="UTC"),
                    "latitude": lat,
                    "longitude": lon,
                    "metric_type": "sea_ice_extent",
                    "product": "nsidc_seaice_index_v4_0",
                    "value": max(0.0, value),
                    "unit": "10^6 km^2",
                    "spatial_scope": "hemispheric",
                    "source_dataset": f"{'N' if north else 'S'}_seaice_extent_daily_v4.0.csv",
                }
            )
            day += timedelta(days=1)

        frame = pd.DataFrame(rows)
        frame = _envelope(
            frame,
            source_id="noaa_nsidc",
            metric_type="sea_ice_extent",
            region_id=region_id,
            resolution=region.h3_resolution,
            ingested_at=ingested_at,
            run_id=run_id,
        )
        frame = validate_frame(conform_to_schema(frame, BRONZE_NOAA_NSIDC), BRONZE_NOAA_NSIDC)
        frame = validate_frame(frame, BRONZE_ENVELOPE)
        _write_partitioned(
            frame, out_root, "noaa_nsidc", region_id, time_column="timestamp", backfill=False
        )

        # The static 30-year baseline, anchored to a non-leap reference year.
        reference = pd.Timestamp("2001-01-01", tz="UTC")
        climatology = []
        for day_of_year in range(1, 367):
            anchor = reference + pd.to_timedelta(min(day_of_year, 365) - 1, unit="D")
            mean = _sea_ice_mean(anchor.date(), north=north)
            climatology.append(
                {
                    "timestamp": anchor,
                    "latitude": lat,
                    "longitude": lon,
                    "metric_type": "sea_ice_extent_climatology",
                    "product": "nsidc_seaice_index_v4_0",
                    "value": mean,
                    "unit": "10^6 km^2",
                    "spatial_scope": "hemispheric",
                    "baseline_stddev": 0.42,
                    "day_of_year": float(day_of_year),
                    "source_dataset": f"{'N' if north else 'S'}_seaice_extent_climatology_1981-2010_v4.0.csv",
                }
            )
        clim_frame = pd.DataFrame(climatology)
        clim_frame = _envelope(
            clim_frame,
            source_id="noaa_nsidc",
            metric_type="sea_ice_extent_climatology",
            region_id=region_id,
            resolution=region.h3_resolution,
            ingested_at=ingested_at,
            run_id=run_id,
        )
        clim_frame = validate_frame(conform_to_schema(clim_frame, BRONZE_NOAA_NSIDC), BRONZE_NOAA_NSIDC)
        clim_frame = validate_frame(clim_frame, BRONZE_ENVELOPE)
        _write_partitioned(
            clim_frame, out_root, "noaa_nsidc", region_id, time_column="timestamp", backfill=True
        )
        rows_written += len(frame) + len(clim_frame)

    sst_regions = [value for value in regions if value in MARINE_REGIONS]
    for region_id in sst_regions:
        region = get_region(region_id)
        rows = []
        month = pd.Period(start, freq="M")
        while month.start_time.date() <= end:
            # A coarse sample of the OISST grid inside the region.
            for lat_step in range(3):
                for lon_step in range(3):
                    west, south, east, north = region.bbox
                    lat = south + (north - south) * (lat_step + 0.5) / 3.0
                    lon = west + (east - west) * (lon_step + 0.5) / 3.0
                    value = rng.gauss(0.0, SST_ANOMALY_NOISE)
                    if inject_anomaly and region_id == MARINE_REGIONS[0] and month == pd.Period(
                        f"{end.year}-08", freq="M"
                    ):
                        value += 3.0  # a marine heatwave
                    rows.append(
                        {
                            "timestamp": pd.Timestamp(min(month.end_time.date(), end), tz="UTC"),
                            "latitude": lat,
                            "longitude": lon,
                            "metric_type": "sst_anomaly",
                            "product": "noaa_oisst_v2_1_anom",
                            "value": value,
                            "unit": "degC",
                            "spatial_scope": "gridded",
                            "source_dataset": f"sst.day.anom.{month.year}.nc",
                        }
                    )
            month += 1

        frame = pd.DataFrame(rows)
        frame = _envelope(
            frame,
            source_id="noaa_nsidc",
            metric_type="sst_anomaly",
            region_id=region_id,
            resolution=region.h3_resolution,
            ingested_at=ingested_at,
            run_id=run_id,
        )
        frame = validate_frame(conform_to_schema(frame, BRONZE_NOAA_NSIDC), BRONZE_NOAA_NSIDC)
        frame = validate_frame(frame, BRONZE_ENVELOPE)
        _write_partitioned(
            frame, out_root, "noaa_nsidc", region_id, time_column="timestamp", backfill=False
        )
        rows_written += len(frame)
    return rows_written


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

def _write_partitioned(
    frame: pd.DataFrame,
    out_root: Path,
    source_id: str,
    region_id: str,
    *,
    time_column: str,
    backfill: bool,
) -> None:
    """Write a landing using the collectors' own partition scheme.

    Paths come from ``collectors.config.bronze_prefix``/``bronze_filename`` rather
    than being re-derived here, so generated data and real collection runs cannot
    drift apart. ``backfill=True`` produces monthly partitions under ``backfill/``
    and merges into an existing file; ``backfill=False`` produces daily partitions.
    """
    stamps = pd.to_datetime(frame[time_column], utc=True)
    period = stamps.dt.tz_convert("UTC").dt.tz_localize(None).dt.to_period("M" if backfill else "D")

    for value in sorted(period.unique()):
        chunk = frame.loc[period == value].reset_index(drop=True)
        day = value.to_timestamp().date()
        partition_day = None if backfill else day.day
        prefix = bronze_prefix(
            source_id,
            region_id=region_id,
            year=day.year,
            month=day.month,
            day=partition_day,
            backfill=backfill,
        )
        filename = bronze_filename(
            source_id,
            region_id,
            year=day.year,
            month=day.month,
            day=partition_day,
            backfill=backfill,
        )
        target = out_root / prefix / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if backfill and target.exists():
            chunk = pd.concat([pd.read_parquet(target), chunk], ignore_index=True)
        chunk.to_parquet(target, index=False, compression="zstd")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="data/bronze", help="bronze root (default: %(default)s)")
    parser.add_argument("--start-date", default=None, help="inclusive start (default: 730 days ago)")
    parser.add_argument("--end-date", default=None, help="inclusive end (default: yesterday)")
    parser.add_argument("--regions", default="all", help="comma list or 'all'")
    parser.add_argument("--seed", type=int, default=20240921)
    parser.add_argument(
        "--inject-anomaly",
        action="store_true",
        help="plant obvious anomalies (fire spike, NDVI crash, ice excursion, marine heatwave)",
    )
    parser.add_argument("--clean", action="store_true", help="delete the output root first")
    args = parser.parse_args(argv)

    end = date.fromisoformat(args.end_date) if args.end_date else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start_date) if args.start_date else end - timedelta(days=729)
    if start > end:
        raise SystemExit(f"--start-date {start} is after --end-date {end}")

    if args.regions.strip().lower() in {"", "all"}:
        regions = tuple(region.region_id for region in REGIONS)
    else:
        regions = tuple(value.strip() for value in args.regions.split(",") if value.strip())

    out_root = Path(args.out)
    if args.clean and out_root.exists():
        import shutil

        shutil.rmtree(out_root)

    rng = _rng(args.seed)
    ingested_at = datetime.now(tz=pd.Timestamp.now().tzinfo)
    run_id = f"synthetic-{args.seed}"

    total = 0
    total += generate_firms(
        out_root,
        regions=tuple(value for value in regions if value in FIRE_REGIONS),
        start=start,
        end=end,
        rng=rng,
        ingested_at=ingested_at,
        run_id=run_id,
        inject_anomaly=args.inject_anomaly,
    )
    total += generate_sentinel(
        out_root,
        regions=tuple(value for value in regions if value in SENTINEL_REGIONS),
        start=start,
        end=end,
        rng=rng,
        ingested_at=ingested_at,
        run_id=run_id,
        inject_anomaly=args.inject_anomaly,
    )
    total += generate_noaa_nsidc(
        out_root,
        regions=regions,
        start=start,
        end=end,
        rng=rng,
        ingested_at=ingested_at,
        run_id=run_id,
        inject_anomaly=args.inject_anomaly,
    )

    files = sorted(out_root.rglob("*.parquet"))
    print(
        f"synthetic bronze: {total} rows across {len(files)} partition(s) under {out_root} "
        f"({start} .. {end}){' [anomalies injected]' if args.inject_anomaly else ''}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
