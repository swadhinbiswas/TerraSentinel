"""Sentinel/GEE collector: band construction, result parsing, and the two grains.

Earth Engine is faked at the `ee` object level. The collector already takes `ee` as a
parameter on its internal methods, so no monkeypatching of `ee.Initialize` is needed —
and the parsing logic that has to survive upstream naming conventions is fully exercised.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from collectors.config import get_region
from collectors.sentinel_gee_collector import (
    S1_PRODUCT,
    S2_PRODUCT,
    SentinelCollector,
    cell_polygon_coords,
    chunked,
    pick_property,
)
from collectors.time_utils import composite_windows
from ops.resilience import RetryPolicy
from tests.conftest import RecordingSleep, StepClock, utc

# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

class TestPickProperty:
    def test_exact_band_name_is_the_mean(self) -> None:
        # Single-reducer reduceRegions names properties after the band.
        assert pick_property({"ndvi_20240930": 0.71}, "ndvi_20240930") == 0.71

    def test_suffixed_reducer_name(self) -> None:
        assert pick_property({"ndvi_20240930_mean": 0.71}, "ndvi_20240930") == 0.71

    def test_prefixed_match_for_prefixed_bands(self) -> None:
        # Year-over-year change bands are named "recent_<date>"/"baseline_<date>".
        props = {"recent_20250915": 0.4, "baseline_20240915": 0.8}
        assert pick_property(props, "recent", "recent_mean") == 0.4
        assert pick_property(props, "baseline", "baseline_mean") == 0.8

    def test_missing_property_returns_none(self) -> None:
        assert pick_property({"other": 1.0}, "ndvi_20240930") is None

    def test_does_not_confuse_neighbouring_window_bands(self) -> None:
        props = {"ndvi_20240930_mean": 0.5, "ndvi_20241031_mean": 0.2}
        assert pick_property(props, "ndvi_20240930") == 0.5
        assert pick_property(props, "ndvi_20241031") == 0.2

    def test_none_values_are_skipped(self) -> None:
        assert pick_property({"a": None, "a_mean": 3.0}, "a") == 3.0


class TestCellGeometry:
    def test_ring_is_closed_and_lon_lat_ordered(self) -> None:
        ring = cell_polygon_coords("851ee193fffffff")
        assert len(ring) == 7
        assert ring[0] == ring[-1]
        assert all(-180.0 <= lon <= 180.0 for lon, _lat in ring)
        assert all(-90.0 <= lat <= 90.0 for _lon, lat in ring)

    def test_ring_is_clockwise_hexagon_sized(self) -> None:
        ring = cell_polygon_coords("851ee193fffffff")
        lons = [point[0] for point in ring]
        assert 0.0 < max(lons) - min(lons) < 3.0


class TestChunked:
    def test_splits_evenly(self) -> None:
        assert list(chunked(list(range(10)), 5)) == [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]]

    def test_keeps_a_short_tail(self) -> None:
        assert list(chunked(list(range(7)), 3)) == [[0, 1, 2], [3, 4, 5], [6]]

    def test_single_chunk_when_smaller_than_size(self) -> None:
        assert list(chunked([1, 2], 10)) == [[1, 2]]

    def test_rejects_zero_size(self) -> None:
        with pytest.raises(ValueError):
            list(chunked([1], 0))


# --------------------------------------------------------------------------
# Fake Earth Engine
# --------------------------------------------------------------------------

class FakeReduce:
    def __init__(self, payload):
        self._payload = payload

    def getInfo(self):
        return self._payload


class FakeImage:
    """Records band composition so parsing can be verified end to end."""

    def __init__(self, bands, value: float = 0.5) -> None:
        self.bands = list(bands)
        self.value = value

    def rename(self, name: str) -> FakeImage:
        return FakeImage([name], self.value)

    def select(self, band: str) -> FakeImage:
        return FakeImage([band], self.value)

    def addBands(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands + other.bands, self.value)

    def subtract(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, self.value)

    def reduceRegion(self, **_kwargs) -> FakeReduce:
        # Emulate the combine() naming: <band>_<reducer>.
        payload: dict[str, float | int] = {}
        for band in self.bands:
            payload[f"{band}_mean"] = self.value
            payload[f"{band}_stdDev"] = 0.1
            payload[f"{band}_count"] = 1234
            payload[f"{band}_min"] = 0.1
            payload[f"{band}_max"] = 0.9
        return FakeReduce(payload)

    def reduceRegions(self, collection, **_kwargs) -> FakeReduce:
        features = [
            {
                "properties": {
                    "h3_index": cell,
                    **dict.fromkeys(self.bands, self.value),
                }
            }
            for cell in collection.cells
        ]
        return FakeReduce({"features": features})


class FakeFeature:
    def __init__(self, geometry, properties) -> None:
        self.geometry = geometry
        self.properties = properties


class FakeFeatureCollection:
    def __init__(self, features) -> None:
        self.features = features
        self.cells = [feature.properties["h3_index"] for feature in features]


class FakeCollection:
    def __init__(self, product: str) -> None:
        self.product = product
        self.band: str | None = None
        self.calls: list[str] = []
        self.windows: list[tuple[str, str]] = []

    def filterBounds(self, geometry) -> FakeCollection:
        self.calls.append("filterBounds")
        return self

    def filter(self, expression) -> FakeCollection:
        self.calls.append(f"filter({expression})")
        return self

    def map(self, _fn) -> FakeCollection:
        self.calls.append("map")
        return self

    def select(self, band: str) -> FakeCollection:
        self.band = band
        return self

    def filterDate(self, start: str, end: str) -> FakeCollection:
        self.windows.append((start, end))
        return self

    def mean(self) -> FakeImage:
        return FakeImage([self.band or "band"])


class FakeFilter:
    @staticmethod
    def lt(field, value) -> str:
        return f"lt:{field}:{value}"

    @staticmethod
    def eq(field, value) -> str:
        return f"eq:{field}:{value}"

    @staticmethod
    def listContains(field, value) -> str:
        return f"listContains:{field}:{value}"


class FakeReducer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.combined: list[str] = []

    def combine(self, reducer2=None, sharedInputs: bool = False) -> FakeReducer:
        self.combined.append(getattr(reducer2, "name", "?"))
        return self


class FakeGeometry:
    def __init__(self, coords) -> None:
        self.coords = coords

    @classmethod
    def Rectangle(cls, coords, **_kwargs) -> FakeGeometry:
        return cls(coords)

    @classmethod
    def Polygon(cls, coords, **_kwargs) -> FakeGeometry:
        return cls(coords)


class FakeEE:
    """The subset of the `ee` surface the collector actually touches."""

    def __init__(self) -> None:
        self.collections: list[FakeCollection] = []
        self.Image = type("Image", (), {"cat": staticmethod(self._cat)})
        self.Geometry = FakeGeometry
        self.Feature = FakeFeature
        self.FeatureCollection = FakeFeatureCollection
        self.Filter = FakeFilter
        self.Reducer = type(
            "Reducer",
            (),
            {
                "mean": staticmethod(lambda: FakeReducer("mean")),
                "stdDev": staticmethod(lambda: FakeReducer("stdDev")),
                "count": staticmethod(lambda: FakeReducer("count")),
                "minMax": staticmethod(lambda: FakeReducer("minMax")),
            },
        )

    @staticmethod
    def _cat(images) -> FakeImage:
        bands: list[str] = []
        for image in images:
            bands.extend(image.bands)
        return FakeImage(bands)

    def ImageCollection(self, product: str) -> FakeCollection:  # noqa: N802 - mirrors ee API
        collection = FakeCollection(product)
        self.collections.append(collection)
        return collection


def sentinel_collector(uploader, staging_dir, **overrides) -> SentinelCollector:
    defaults = dict(
        uploader=uploader,
        staging_dir=staging_dir,
        sleep=RecordingSleep(),
        rng=random.Random(11),
        now=StepClock(utc(2024, 9, 1, 3, 0), utc(2024, 9, 1, 3, 0, 45)),
        run_id="run-sentinel-test01",
        policy=RetryPolicy(attempts=2, base_delay=0.01, jitter="none"),
        include_grid=False,
    )
    defaults.update(overrides)
    return SentinelCollector(**defaults)


# --------------------------------------------------------------------------
# Collection wiring
# --------------------------------------------------------------------------

class TestCollectionSelection:
    def test_deforestation_uses_sentinel2_ndvi_with_cloud_filter(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(uploader, tmp_staging)

        collection = collector._collection(ee, get_region("carpathian_deforest"), S2_PRODUCT)

        assert ee.collections[0].product == "COPERNICUS/S2_SR_HARMONIZED"
        assert collection.band == "ndvi"
        joined = " ".join(collection.calls)
        assert "CLOUDY_PIXEL_PERCENTAGE" in joined
        assert collection.calls.count("map") == 2  # cloud mask, then NDVI

    def test_ice_uses_sentinel1_vv_sar(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(uploader, tmp_staging)

        collection = collector._collection(ee, get_region("alps_ice"), S1_PRODUCT)

        assert ee.collections[0].product == "COPERNICUS/S1_GRD"
        assert ee.collections[0].band == "VV"
        joined = " ".join(collection.calls)
        assert "instrumentMode" in joined
        assert "transmitterReceiverPolarisation" in joined

    def test_region_series_builds_one_band_per_window(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(uploader, tmp_staging)
        windows = composite_windows(date(2024, 7, 1), date(2024, 9, 30), "M")

        stack = collector._stack(
            ee, collector._collection(ee, get_region("carpathian_deforest"), S2_PRODUCT), windows, "ndvi"
        )

        assert stack.bands == ["ndvi_20240731", "ndvi_20240831", "ndvi_20240930"]

    def test_composite_windows_are_half_open_periods(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(uploader, tmp_staging)
        collection = collector._collection(ee, get_region("carpathian_deforest"), S2_PRODUCT)

        collector._composite(collection, (date(2024, 8, 1), date(2024, 8, 31)), "ndvi")

        start, end = collection.windows[0]
        assert start == "2024-08-01"
        assert end == "2024-09-01"  # exclusive end so the last day is included


class TestRegionSeriesParsing:
    def test_parses_every_window_into_a_region_row(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(uploader, tmp_staging)
        region = get_region("carpathian_deforest")
        windows = composite_windows(date(2024, 7, 1), date(2024, 9, 30), "M")

        frame = collector._region_series(ee, region, windows, product=S2_PRODUCT, metric="ndvi")

        assert len(frame) == 3
        assert set(frame["metric_type"]) == {"ndvi"}
        assert set(frame["spatial_scope"]) == {"region"}
        assert set(frame["region_id"]) == {"carpathian_deforest"}
        assert set(frame["product"]) == {S2_PRODUCT}
        # Every window's statistics resolved, not just the first band.
        assert frame["mean"].notna().all()
        assert frame["observation_count"].tolist() == [1234, 1234, 1234]
        assert frame["stddev"].notna().all()
        assert frame["min"].notna().all() and frame["max"].notna().all()

    def test_region_rows_sit_on_the_region_centroid(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(uploader, tmp_staging)
        region = get_region("alps_ice")

        frame = collector._region_series(
            ee,
            region,
            composite_windows(date(2024, 8, 1), date(2024, 8, 31), "M"),
            product=S1_PRODUCT,
            metric="sar_backscatter",
        )

        assert region.contains(frame["latitude"].iloc[0], frame["longitude"].iloc[0])

    def test_empty_reduction_returns_no_rows(self, uploader, tmp_staging) -> None:
        class EmptyEE(FakeEE):
            pass

        ee = EmptyEE()
        collector = sentinel_collector(uploader, tmp_staging)
        original = FakeImage.reduceRegion
        def empty_reduction(_image, **_kwargs):
            return FakeReduce({})

        FakeImage.reduceRegion = empty_reduction  # type: ignore[method-assign]
        try:
            frame = collector._region_series(
                ee,
                get_region("alps_ice"),
                composite_windows(date(2024, 8, 1), date(2024, 8, 31), "M"),
                product=S1_PRODUCT,
                metric="sar_backscatter",
            )
        finally:
            FakeImage.reduceRegion = original  # type: ignore[assignment]

        assert frame.empty


class TestGridChange:
    def test_emits_one_row_per_h3_cell_with_preset_indices(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(
            uploader, tmp_staging, include_grid=True, max_cells_per_request=1200
        )
        region = get_region("carpathian_deforest")

        frame = collector._grid_change(
            ee,
            region,
            composite_windows(date(2024, 8, 1), date(2024, 8, 31), "M"),
            product=S2_PRODUCT,
            metric="ndvi",
        )

        assert len(frame) > 900  # res-5 cells covering the Carpathians
        assert set(frame["spatial_scope"]) == {"grid"}
        assert set(frame["region_id"]) == {"carpathian_deforest"}
        assert frame["h3_index"].map(lambda cell: len(str(cell)) == 15).all()
        # Change rows carry the year-over-year delta, not a sample count.
        assert frame["mean"].notna().all()

    def test_grid_batches_are_chunked(self, uploader, tmp_staging) -> None:
        batches: list[int] = []
        ee = FakeEE()
        original = ee.FeatureCollection

        def counting(features):
            batches.append(len(features))
            return original(features)

        ee.FeatureCollection = counting
        collector = sentinel_collector(
            uploader, tmp_staging, include_grid=True, max_cells_per_request=300
        )
        collector._grid_change(
            ee,
            get_region("carpathian_deforest"),
            composite_windows(date(2024, 8, 1), date(2024, 8, 31), "M"),
            product=S2_PRODUCT,
            metric="ndvi",
        )

        assert all(size <= 300 for size in batches)
        assert len(batches) > 1

    def test_change_stack_carries_recent_baseline_and_delta(self, uploader, tmp_staging) -> None:
        ee = FakeEE()
        collector = sentinel_collector(uploader, tmp_staging, include_grid=True, max_cells_per_request=2000)
        collector._grid_change(
            ee,
            get_region("carpathian_deforest"),
            composite_windows(date(2024, 8, 1), date(2024, 8, 31), "M"),
            product=S2_PRODUCT,
            metric="ndvi",
        )

        collection = ee.collections[0]
        # recent window, then the same window one year earlier
        assert collection.windows[0][0] == "2024-08-01"
        # 2024 is a leap year, so "same window last year" is 2 Aug - 1 Sep.
        assert collection.windows[1][0] == "2023-08-02"
        assert collection.windows[1][1] == "2023-09-02"


class TestAuthRecovery:
    def test_reauthenticates_once_on_an_auth_error(self, uploader, tmp_staging, monkeypatch) -> None:
        collector = sentinel_collector(uploader, tmp_staging)
        refreshes: list[str] = []
        monkeypatch.setattr(
            "collectors.sentinel_gee_collector.refresh_ee_credentials",
            lambda: refreshes.append("refreshed"),
        )
        monkeypatch.setattr("collectors.sentinel_gee_collector.initialize_ee", lambda: None)

        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("invalid_grant: token expired")
            return {"ok": True}

        assert collector._gee_call(flaky, description="test") == {"ok": True}
        assert refreshes == ["refreshed"]

    def test_non_auth_error_becomes_transient(self, uploader, tmp_staging) -> None:
        from ops.resilience import TransientError

        collector = sentinel_collector(uploader, tmp_staging)

        def broken():
            raise RuntimeError("computation timed out")

        with pytest.raises(TransientError, match="computation timed out"):
            collector._gee_call(broken, description="test")

    def test_repeated_auth_failure_is_reported(self, uploader, tmp_staging, monkeypatch) -> None:
        from ops.resilience import TransientError

        collector = sentinel_collector(uploader, tmp_staging)
        monkeypatch.setattr("collectors.sentinel_gee_collector.refresh_ee_credentials", lambda: None)
        monkeypatch.setattr("collectors.sentinel_gee_collector.initialize_ee", lambda: None)

        def always_expired():
            raise RuntimeError("invalid_grant: expired")

        with pytest.raises(TransientError, match="after re-auth"):
            collector._gee_call(always_expired, description="test")


class TestFetchDispatch:
    def test_uses_weekly_window_for_live_and_monthly_for_backfill(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        collector = sentinel_collector(uploader, tmp_staging, include_grid=False)
        monkeypatch.setattr("collectors.sentinel_gee_collector.initialize_ee", lambda: FakeEE())
        captured: dict[str, list] = {}

        def fake_series(_self, ee, region, windows, *, product, metric):
            captured[product] = windows
            import pandas as pd

            return pd.DataFrame()

        monkeypatch.setattr(SentinelCollector, "_region_series", fake_series)

        collector.fetch(get_region("carpathian_deforest"), start_date=date(2024, 8, 1), end_date=date(2024, 8, 21))
        live_windows = captured[S2_PRODUCT]
        assert (live_windows[0][1] - live_windows[0][0]).days == 6  # 7-day window

        captured.clear()
        collector.fetch(
            get_region("carpathian_deforest"),
            start_date=date(2024, 7, 1),
            end_date=date(2024, 8, 31),
            backfill=True,
        )
        backfill_windows = captured[S2_PRODUCT]
        assert len(backfill_windows) == 2  # calendar months

    def test_ice_regions_use_sar(self, uploader, tmp_staging, monkeypatch) -> None:
        import pandas as pd

        collector = sentinel_collector(uploader, tmp_staging, include_grid=False)
        monkeypatch.setattr("collectors.sentinel_gee_collector.initialize_ee", lambda: FakeEE())
        seen: list[str] = []

        def fake_series(_self, ee, region, windows, *, product, metric):
            seen.append(product)
            return pd.DataFrame()

        monkeypatch.setattr(SentinelCollector, "_region_series", fake_series)

        collector.fetch(get_region("alps_ice"), start_date=date(2024, 8, 1), end_date=date(2024, 8, 7))

        assert seen == [S1_PRODUCT]
