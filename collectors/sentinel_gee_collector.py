"""Copernicus Sentinel-2 NDVI / Sentinel-1 SAR collector via Google Earth Engine.

Two spatial grains, chosen because they answer different questions at very
different costs:

* **region grain** — one ``reduceRegion`` call over the region geometry against a
  multi-band composite stack, returning a full mean/stddev/min/max/time series.
  One request covers an entire multi-year backfill, and this series is what the
  deforestation autoencoder actually consumes.
* **grid grain** — a year-over-year change map on a fixed H3 grid (resolution 5,
  ~253 km^2 cells) for the dashboard heatmap. This is deliberately *not* per-pixel
  H3: bucketing Sentinel at resolution 7 would mean 200k+ polygons per region in a
  single ``reduceRegions`` call, which neither the free-tier quota nor an Actions
  runner can absorb. Coarse cells plus a change value give a map that is honest
  about its own resolution.

Raw imagery never leaves Earth Engine. Only extracted statistics are stored, which
is the whole reason this fits in a free Hugging Face dataset repo.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import date, timedelta
from typing import Any

import h3
import pandas as pd

from collectors.base_collector import BaseCollector, main_for
from collectors.config import Region
from collectors.geo_utils import cell_center, cells_for_bbox
from collectors.time_utils import composite_windows
from ops.resilience import PermanentError, TransientError
from pandera_schemas import BRONZE_SENTINEL

LOGGER = logging.getLogger(__name__)

NDVI_METRIC = "ndvi"
SAR_METRIC = "sar_backscatter"

#: Stored product labels — what lands in bronze and what dbt models filter on.
S2_PRODUCT = "S2_SR_HARMONIZED"
S1_PRODUCT = "S1_GRD"

#: Earth Engine asset ids. These are NOT the stored labels: GEE requires the full
#: "COPERNICUS/..." path, and passing a bare label fails at request time.
S2_COLLECTION_ID = "COPERNICUS/S2_SR_HARMONIZED"
S1_COLLECTION_ID = "COPERNICUS/S1_GRD"

#: H3 resolution for the change-map grain (~253 km^2 per cell).
GEE_GRID_RESOLUTION = 5
#: Analysis scale for zonal reductions, in metres. Sentinel NDVI is nominally 10 m;
#: 250 m is the scale at which a multi-decade regional trend is statistically
#: meaningful and computationally sane.
GEE_REDUCE_SCALE_M = 250
#: Cap on polygons per FeatureCollection sent to reduceRegions.
GEE_MAX_CELLS_PER_REQUEST = 500
GEE_TILE_SCALE = 8
#: Maximum cloud cover for a Sentinel-2 scene to enter a composite.
S2_MAX_CLOUD_PCT = 60

_AUTH_ERROR_TOKENS = ("invalid_grant", "expired", "unauthorized", "401", "credential")

_EE_READY = False


def initialize_ee() -> Any:
    """Initialise Earth Engine from the service-account secret, once per process.

    ``google-auth`` refreshes the access token on each call, so a long backfill
    does not go stale mid-run; :func:`refresh_ee_credentials` handles the rarer
    case where the credential itself is rejected.
    """
    global _EE_READY
    try:
        import ee
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise PermanentError(
            "the Sentinel collector needs earthengine-api: "
            "install with `uv pip install -e '.[gee]'`"
        ) from exc

    if not _EE_READY:
        from collectors.config import require_secrets

        secrets = require_secrets(
            ["GEE_SERVICE_ACCOUNT_JSON", "GEE_SERVICE_ACCOUNT_EMAIL", "GEE_PROJECT"]
        )
        credentials = ee.ServiceAccountCredentials(
            secrets["GEE_SERVICE_ACCOUNT_EMAIL"], secrets["GEE_SERVICE_ACCOUNT_JSON"]
        )
        ee.Initialize(credentials=credentials, project=secrets["GEE_PROJECT"])
        _EE_READY = True
    return ee


def refresh_ee_credentials() -> None:
    """Force re-initialisation on the next call, e.g. after a rejected token."""
    global _EE_READY
    _EE_READY = False


def cell_polygon_coords(cell: str) -> list[list[float]]:
    """H3 cell boundary as a closed ``[[lon, lat], ...]`` ring for Earth Engine."""
    ring = list(h3.cell_to_boundary(cell))
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return [[float(lon), float(lat)] for lat, lon in ring]


def chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    if size < 1:
        raise ValueError(f"chunk size must be >= 1, got {size}")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def pick_property(properties: dict[str, Any], *names: str) -> float | None:
    """Read a reduceRegions property tolerantly.

    Earth Engine names multi-band reduction outputs either after the band
    (``ndvi_20240930``) or as ``<band>_<reducer>`` depending on the reducer shape.
    Accepting both means a reducer change upstream cannot silently null out a
    whole column.
    """
    for name in names:
        value = properties.get(name)
        if value is not None:
            return float(value)
    for key, value in properties.items():
        if value is None:
            continue
        head = key.split("_", 1)[0]
        for name in names:
            if key == name or head == name or key.startswith(f"{name}_"):
                return float(value)
    return None


class SentinelCollector(BaseCollector):
    """Sentinel-2 NDVI and Sentinel-1 SAR statistics from Google Earth Engine."""

    source_id = "sentinel"
    metric_type = ""
    label = "Copernicus Sentinel (GEE)"
    raw_schema = BRONZE_SENTINEL
    time_column = "window_end"
    default_region_ids = ("carpathian_deforest", "alps_ice", "norway_ice")

    live_lookback_days = 28
    composite_period_live = "W"
    composite_period_backfill = "M"

    def __init__(
        self,
        *,
        include_grid: bool = True,
        grid_resolution: int = GEE_GRID_RESOLUTION,
        reduce_scale_m: int = GEE_REDUCE_SCALE_M,
        max_cells_per_request: int = GEE_MAX_CELLS_PER_REQUEST,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.include_grid = include_grid
        self.grid_resolution = grid_resolution
        self.reduce_scale_m = reduce_scale_m
        self.max_cells_per_request = max_cells_per_request

    # -- fetch -------------------------------------------------------------

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
        period = self.composite_period_backfill if backfill else self.composite_period_live
        windows = composite_windows(start, end, period)

        if region.anomaly_type == "deforestation":
            product, metric = S2_PRODUCT, NDVI_METRIC
        else:
            product, metric = S1_PRODUCT, SAR_METRIC

        self.log(
            logging.INFO,
            f"sentinel/{region.region_id}: {metric} via {product}, {len(windows)} "
            f"{period} window(s) from {start} to {end}",
            source_id=self.source_id,
            region_id=region.region_id,
        )

        ee = initialize_ee()
        frames: list[pd.DataFrame] = [
            self._region_series(ee, region, windows, product=product, metric=metric)
        ]
        if self.include_grid:
            frames.append(
                self._grid_change(ee, region, windows, product=product, metric=metric)
            )

        usable = [frame for frame in frames if frame is not None and not frame.empty]
        if not usable:
            return pd.DataFrame()
        return pd.concat(usable, ignore_index=True)

    # -- collection --------------------------------------------------------

    def _collection(self, ee: Any, region: Region, product: str) -> Any:
        geometry = ee.Geometry.Rectangle(list(region.bbox), proj="EPSG:4326", geodesic=False)
        if product == S2_PRODUCT:

            def mask_clouds(image: Any) -> Any:
                qa = image.select("QA60")
                # QA60 bit 10 = opaque cloud, bit 11 = cirrus.
                clear = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
                return image.updateMask(clear)

            def add_ndvi(image: Any) -> Any:
                return image.addBands(image.normalizedDifference(["B8", "B4"]).rename(NDVI_METRIC))

            return (
                ee.ImageCollection(S2_COLLECTION_ID)
                .filterBounds(geometry)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", S2_MAX_CLOUD_PCT))
                .map(mask_clouds)
                .map(add_ndvi)
                .select(NDVI_METRIC)
            )

        # Sentinel-1 GRD: IW mode, VV polarisation. SAR sees through cloud and
        # polar night, which is why it carries the ice arm.
        return (
            ee.ImageCollection(S1_COLLECTION_ID)
            .filterBounds(geometry)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
            .select("VV")
        )

    def _composite(self, collection: Any, window: tuple[date, date], band: str) -> Any:
        start, end = window
        filtered = collection.filterDate(
            start.isoformat(), (end + timedelta(days=1)).isoformat()
        )
        return filtered.mean().rename(f"{band}_{end:%Y%m%d}")

    def _stack(
        self, ee: Any, collection: Any, windows: Sequence[tuple[date, date]], band: str
    ) -> Any:
        images = [self._composite(collection, window, band) for window in windows]
        if len(images) == 1:
            return images[0]
        try:
            return ee.Image.cat(images)
        except Exception as exc:  # noqa: BLE001 - EE raises plain exceptions
            raise PermanentError(
                f"could not build a {len(windows)}-band composite stack from {band}: {exc}"
            ) from exc

    def _region_series(
        self,
        ee: Any,
        region: Region,
        windows: Sequence[tuple[date, date]],
        *,
        product: str,
        metric: str,
    ) -> pd.DataFrame:
        collection = self._collection(ee, region, product)
        stack = self._stack(ee, collection, windows, metric)
        geometry = ee.Geometry.Rectangle(list(region.bbox), proj="EPSG:4326", geodesic=False)

        reducer = (
            ee.Reducer.mean()
            .combine(reducer2=ee.Reducer.stdDev(), sharedInputs=True)
            .combine(reducer2=ee.Reducer.count(), sharedInputs=True)
            .combine(reducer2=ee.Reducer.minMax(), sharedInputs=True)
        )

        def run() -> dict[str, Any]:
            return stack.reduceRegion(
                reducer=reducer,
                geometry=geometry,
                scale=self.reduce_scale_m,
                maxPixels=int(1e9),
                tileScale=GEE_TILE_SCALE,
                bestEffort=True,
            ).getInfo()

        statistics = self._gee_call(run, description=f"region series {region.region_id}")
        if not statistics:
            self.log(
                logging.WARNING,
                f"sentinel/{region.region_id}: region reduction returned no statistics",
                region_id=region.region_id,
            )
            return pd.DataFrame()

        centre_lat, centre_lon = cell_center(h3.latlng_to_cell(
            (region.bbox[1] + region.bbox[3]) / 2.0,
            (region.bbox[0] + region.bbox[2]) / 2.0,
            self.grid_resolution,
        ))

        rows: list[dict[str, Any]] = []
        for window in windows:
            band = f"{metric}_{window[1]:%Y%m%d}"
            row: dict[str, Any] = {
                "window_start": pd.Timestamp(window[0], tz="UTC"),
                "window_end": pd.Timestamp(window[1], tz="UTC"),
                "latitude": centre_lat,
                "longitude": centre_lon,
                "product": product,
                "metric_type": metric,
                "spatial_scope": "region",
                "mean": pick_property(statistics, band, f"{band}_mean"),
                "stddev": pick_property(statistics, f"{band}_stdDev", f"{band}_stddev"),
                "min": pick_property(statistics, f"{band}_min"),
                "max": pick_property(statistics, f"{band}_max"),
                "observation_count": pick_property(statistics, f"{band}_count"),
                "region_id": region.region_id,
                "source_dataset": product,
            }
            rows.append(row)

        frame = pd.DataFrame(rows)
        frame["observation_count"] = pd.to_numeric(
            frame["observation_count"], errors="coerce"
        )
        return frame

    def _grid_change(
        self,
        ee: Any,
        region: Region,
        windows: Sequence[tuple[date, date]],
        *,
        product: str,
        metric: str,
    ) -> pd.DataFrame:
        """Year-over-year change per H3 cell — the dashboard's deforestation/ice map."""
        recent = windows[-1]
        baseline = (
            recent[0] - timedelta(days=365),
            recent[1] - timedelta(days=365),
        )

        collection = self._collection(ee, region, product)
        recent_image = self._composite(collection, recent, "recent")
        baseline_image = self._composite(collection, baseline, "baseline")
        stacked = recent_image.addBands(baseline_image).addBands(
            recent_image.subtract(baseline_image).rename("change")
        )

        cells = cells_for_bbox(region.bbox, self.grid_resolution)
        rows: list[dict[str, Any]] = []
        for batch in chunked(cells, self.max_cells_per_request):
            features = ee.FeatureCollection(
                [
                    ee.Feature(ee.Geometry.Polygon(cell_polygon_coords(cell)), {"h3_index": cell})
                    for cell in batch
                ]
            )

            def run(batch_features: Any = features) -> dict[str, Any]:
                return stacked.reduceRegions(
                    collection=batch_features,
                    reducer=ee.Reducer.mean(),
                    scale=self.reduce_scale_m,
                    tileScale=GEE_TILE_SCALE,
                ).getInfo()

            payload = self._gee_call(
                run, description=f"grid change {region.region_id} ({len(batch)} cells)"
            )
            for feature in payload.get("features", []):
                properties = feature.get("properties", {}) or {}
                change = pick_property(properties, "change", "change_mean")
                if change is None:
                    continue
                cell = properties.get("h3_index")
                if not cell:
                    continue
                cell_lat, cell_lon = cell_center(str(cell))
                rows.append(
                    {
                        "window_start": pd.Timestamp(recent[0], tz="UTC"),
                        "window_end": pd.Timestamp(recent[1], tz="UTC"),
                        "latitude": cell_lat,
                        "longitude": cell_lon,
                        "h3_index": str(cell),
                        "product": product,
                        "metric_type": metric,
                        "spatial_scope": "grid",
                        "mean": change,
                        "baseline_mean": pick_property(properties, "baseline", "baseline_mean"),
                        "recent_mean": pick_property(properties, "recent", "recent_mean"),
                        "region_id": region.region_id,
                        "source_dataset": product,
                    }
                )

        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        # Grid rows are already attributed to their H3 cell; keep the explicit region
        # so a cell straddling a boundary is never silently reassigned.
        frame["region_id"] = region.region_id
        return frame

    # -- error handling ----------------------------------------------------

    def _gee_call(self, fn: Callable[[], Any], *, description: str) -> Any:
        """Run a server-side call, re-authenticating once on an auth rejection."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - EE raises plain exceptions
            message = str(exc)
            if any(token in message.lower() for token in _AUTH_ERROR_TOKENS):
                self.log(
                    logging.WARNING,
                    f"sentinel: re-authenticating Earth Engine after: {message}",
                    source_id=self.source_id,
                )
                refresh_ee_credentials()
                initialize_ee()
                try:
                    return fn()
                except Exception as retry_exc:  # noqa: BLE001
                    raise TransientError(
                        f"Earth Engine call failed after re-auth ({description}): {retry_exc}"
                    ) from retry_exc
            raise TransientError(f"Earth Engine call failed ({description}): {message}") from exc


if __name__ == "__main__":
    raise SystemExit(main_for(SentinelCollector))
