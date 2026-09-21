"""Collector behaviour: parsing, retries, breakers, partitioning, failure modes.

Every test mocks HTTP at the ``requests`` layer, so the real retry/backoff/breaker
and redaction code is exercised rather than stubbed out.
"""

from __future__ import annotations

import io
import random
import re
from datetime import date, timedelta

import pandas as pd
import pytest
import responses

from collectors.config import MissingCredential, get_region, preflight_sources
from collectors.firms_collector import FirmsCollector, normalise_confidence, product_plan
from collectors.noaa_nsidc_collector import (
    NoaaNsidcCollector,
    _stride_for,
    read_csv_after_header,
)
from ops.resilience import CircuitBreaker, PermanentError, RetryPolicy
from tests.conftest import (
    FIRMS_ERROR_TEXT,
    FIRMS_MODIS_CSV,
    FIRMS_VIIRS_CSV,
    NSIDC_CLIMATOLOGY_CSV,
    NSIDC_DAILY_CSV,
    FakeMonotonic,
    RecordingSleep,
    StepClock,
    utc,
)

MAP_KEY = "testmapkey0000111122223333444455"
FIRMS_URL = re.compile(r"https://firms\.modaps\.eosdis\.nasa\.gov/api/area/csv/.*")
#: Product-specific matchers: the appending API means several products are called
#: in one run, and a catch-all matcher would feed the wrong payload to one of them.
FIRMS_VIIRS_URL = re.compile(r".*/VIIRS_[A-Z0-9]+_(?:NRT|SP)/.*")
FIRMS_MODIS_URL = re.compile(r".*/MODIS_(?:NRT|SP)/.*")
FIRMS_VIIRS_SP = re.compile(r".*/VIIRS_[A-Z0-9]+_SP/.*")
FIRMS_VIIRS_NRT = re.compile(r".*/VIIRS_[A-Z0-9]+_NRT/.*")
FIRMS_MODIS_SP = re.compile(r".*/MODIS_SP/.*")
FIRMS_MODIS_NRT = re.compile(r".*/MODIS_NRT/.*")
NSIDC_NORTH_URL = re.compile(r".*G02135/north/daily/data/N_seaice_extent.*")
NSIDC_SOUTH_URL = re.compile(r".*G02135/south/daily/data/S_seaice_extent.*")


def firms_collector(uploader, staging_dir, **overrides):
    defaults = dict(
        map_key=MAP_KEY,
        uploader=uploader,
        staging_dir=staging_dir,
        sleep=RecordingSleep(),
        rng=random.Random(7),
        now=StepClock(utc(2024, 8, 17, 6, 0), utc(2024, 8, 17, 6, 0, 12)),
        run_id="run-firms-test01",
        request_pause_s=0.0,
        max_requests=200,
        policy=RetryPolicy(attempts=3, base_delay=0.01, jitter="none"),
    )
    defaults.update(overrides)
    return FirmsCollector(**defaults)


def nsidc_collector(uploader, staging_dir, **overrides):
    defaults = dict(
        uploader=uploader,
        staging_dir=staging_dir,
        sleep=RecordingSleep(),
        rng=random.Random(7),
        now=StepClock(utc(2024, 9, 16, 4, 0), utc(2024, 9, 16, 4, 0, 20)),
        run_id="run-noaa-test01",
        policy=RetryPolicy(attempts=2, base_delay=0.01, jitter="none"),
    )
    defaults.update(overrides)
    return NoaaNsidcCollector(**defaults)


def register_firms_products() -> None:
    responses.add(responses.GET, FIRMS_VIIRS_URL, body=FIRMS_VIIRS_CSV, status=200)
    responses.add(responses.GET, FIRMS_MODIS_URL, body=FIRMS_MODIS_CSV, status=200)


def register_firms_failure(*, status: int = 500, body: str = "") -> None:
    responses.add(responses.GET, FIRMS_VIIRS_URL, body=body, status=status)
    responses.add(responses.GET, FIRMS_MODIS_URL, body=body, status=status)


# --------------------------------------------------------------------------
# Confidence normalisation
# --------------------------------------------------------------------------

class TestNormaliseConfidence:
    @pytest.mark.parametrize(
        "value,level",
        [("l", "low"), ("L", "low"), ("n", "nominal"), ("h", "high"), ("HIGH", "high")],
    )
    def test_categorical_viirs_values(self, value: str, level: str) -> None:
        decoded_level, pct = normalise_confidence(value)
        assert decoded_level == level
        assert 0.0 <= pct <= 100.0

    @pytest.mark.parametrize(
        "value,level",
        [(5, "low"), (29, "low"), (50, "nominal"), (79, "nominal"), (80, "high"), (100, "high")],
    )
    def test_numeric_modis_values_map_to_bands(self, value: int, level: str) -> None:
        decoded_level, pct = normalise_confidence(value)
        assert decoded_level == level
        assert pct == float(value)

    def test_clamps_out_of_range_percentages(self) -> None:
        assert normalise_confidence("140")[1] == 100.0
        assert normalise_confidence("-5")[1] == 0.0

    def test_unparseable_value_fails_loudly(self) -> None:
        # Defaulting to a mid-range guess would quietly skew the model's input.
        with pytest.raises(PermanentError, match="unrecognised FIRMS confidence"):
            normalise_confidence("uncertain")

    def test_missing_value_fails_loudly(self) -> None:
        with pytest.raises(PermanentError, match="missing its confidence"):
            normalise_confidence(None)


# --------------------------------------------------------------------------
# FIRMS
# --------------------------------------------------------------------------

class TestFirmsCollector:
    @responses.activate
    def test_live_run_produces_validated_partitions(self, uploader, tmp_staging) -> None:
        register_firms_products()
        collector = firms_collector(uploader, tmp_staging)

        summary = collector.run([get_region("iberia_fire")])

        assert summary.exit_code() == 0
        result = summary.results[0]
        assert result.status == "success"
        # 2 VIIRS products x 4 rows + 1 MODIS product x 1 row
        assert result.rows == 9

        # Two detections days in the payload => two daily partitions.
        assert result.details["partition_count"] == 2
        paths = uploader.paths
        assert paths == [
            "firms/region=iberia_fire/year=2024/month=08/day=15/firms_iberia_fire_20240815.parquet",
            "firms/region=iberia_fire/year=2024/month=08/day=16/firms_iberia_fire_20240816.parquet",
        ]

    @responses.activate
    def test_partition_contents_carry_h3_and_provenance(self, uploader, tmp_staging) -> None:
        register_firms_products()
        collector = firms_collector(uploader, tmp_staging)
        collector.run([get_region("iberia_fire")])

        frame = pd.read_parquet(
            io.BytesIO(
                uploader.files[
                    "firms/region=iberia_fire/year=2024/month=08/day=15/"
                    "firms_iberia_fire_20240815.parquet"
                ]
            )
        )
        assert set(frame["source_id"]) == {"firms"}
        assert set(frame["region_id"]) == {"iberia_fire"}
        assert set(frame["run_id"]) == {"run-firms-test01"}
        assert set(frame["metric_type"]) == {"fire_radiative_power"}
        assert set(frame["confidence"]).issubset({"low", "nominal", "high"})
        assert frame["h3_index"].map(lambda cell: isinstance(cell, str) and len(cell) > 10).all()
        assert frame["acq_datetime"].dt.tz is not None
        assert frame["ingested_at"].dt.tz is not None
        # Both instruments land in the same partition, which is what lets the
        # staging model dedupe a fire detected by several overpasses.
        assert set(frame["product"]) == {"VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT"}

    @responses.activate
    def test_request_url_is_well_formed(self, uploader, tmp_staging) -> None:
        register_firms_products()
        collector = firms_collector(uploader, tmp_staging)
        collector.run([get_region("iberia_fire")], start_date=date(2024, 8, 15), end_date=date(2024, 8, 15))

        url = responses.calls[0].request.url
        assert f"/{MAP_KEY}/" in url
        assert "/VIIRS_SNPP_NRT/-10.0,35.5,3.5,43.9/1/2024-08-15" in url

    @responses.activate
    def test_historical_windows_use_standard_processing(self, uploader, tmp_staging) -> None:
        register_firms_products()
        # Clock well past the SP lag, so this window is old enough for the archive.
        collector = firms_collector(
            uploader,
            tmp_staging,
            now=StepClock(utc(2025, 1, 15, 6, 0), utc(2025, 1, 15, 6, 0, 12)),
        )

        summary = collector.run(
            [get_region("iberia_fire")],
            start_date=date(2024, 8, 1),
            end_date=date(2024, 8, 10),
            backfill=True,
        )

        assert summary.exit_code() == 0
        requested = " ".join(call.request.url for call in responses.calls)
        assert "_SP/" in requested
        assert "_NRT/" not in requested

        # One commit for the whole backfill rather than one per partition.
        assert len(uploader.folders) == 1
        folder, path_in_repo = uploader.folders[0]
        assert path_in_repo == "backfill/firms"
        # Monthly, not daily: a 5-day window landing mid-month must not create
        # daily paths under backfill/.
        assert sorted(uploader.paths) == [
            "backfill/firms/region=iberia_fire/year=2024/month=08/firms_iberia_fire_202408.parquet"
        ]

    @responses.activate
    def test_recent_windows_use_near_real_time(self, uploader, tmp_staging) -> None:
        # Against the 2024-08-17 clock, August 2024 is too new for standard
        # processing to exist yet — the case that silently returned nothing when
        # product choice was driven by "is this a backfill?".
        register_firms_products()
        collector = firms_collector(uploader, tmp_staging)

        summary = collector.run(
            [get_region("iberia_fire")],
            start_date=date(2024, 8, 5),
            end_date=date(2024, 8, 9),
            backfill=True,
        )

        assert summary.exit_code() == 0
        assert summary.total_rows == 9  # 2 VIIRS products x 4 rows + MODIS x 1
        requested = " ".join(call.request.url for call in responses.calls)
        assert "_NRT/" in requested
        assert "_SP/" not in requested

    @responses.activate
    def test_falls_back_to_the_other_archive_when_the_preferred_one_is_empty(
        self, uploader, tmp_staging
    ) -> None:
        # A boundary window: SP is preferred, but the archive has not caught up yet.
        responses.add(responses.GET, FIRMS_VIIRS_SP, body="latitude,longitude\n", status=200)
        responses.add(responses.GET, FIRMS_MODIS_SP, body="latitude,longitude\n", status=200)
        responses.add(responses.GET, FIRMS_VIIRS_NRT, body=FIRMS_VIIRS_CSV, status=200)
        responses.add(responses.GET, FIRMS_MODIS_NRT, body=FIRMS_MODIS_CSV, status=200)
        collector = firms_collector(uploader, tmp_staging)

        # ~106 days before the 2024-08-17 clock: inside the band where SP is
        # preferred but may not have caught up, and NRT is still retained.
        summary = collector.run(
            [get_region("iberia_fire")],
            start_date=date(2024, 5, 1),
            end_date=date(2024, 5, 3),
            backfill=True,
        )

        assert summary.exit_code() == 0
        assert summary.total_rows == 9  # SP empty, NRT used instead
        requested = " ".join(call.request.url for call in responses.calls)
        assert "_SP/" in requested and "_NRT/" in requested

    @responses.activate
    def test_long_range_is_split_into_five_day_windows(self, uploader, tmp_staging) -> None:
        register_firms_products()
        collector = firms_collector(uploader, tmp_staging)

        collector.run(
            [get_region("iberia_fire")],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 10),
            backfill=True,
        )

        # 10 days / 5-day cap = 2 windows, times 3 archive products.
        assert len(responses.calls) == 6
        day_ranges = sorted({call.request.url.rsplit("/", 2)[-2] for call in responses.calls})
        assert day_ranges == ["5"]

    @responses.activate
    def test_transient_failures_are_retried(self, uploader, tmp_staging) -> None:
        # Fail the first attempt twice for one product, then succeed.
        responses.add(responses.GET, FIRMS_VIIRS_URL, status=503)
        responses.add(responses.GET, FIRMS_VIIRS_URL, status=503)
        responses.add(responses.GET, FIRMS_VIIRS_URL, body=FIRMS_VIIRS_CSV, status=200)
        responses.add(responses.GET, FIRMS_MODIS_URL, body=FIRMS_MODIS_CSV, status=200)

        collector = firms_collector(uploader, tmp_staging)
        summary = collector.run(
            [get_region("iberia_fire")], start_date=date(2024, 8, 15), end_date=date(2024, 8, 15)
        )

        assert summary.exit_code() == 0
        assert summary.total_rows == 9
        # 3 products, plus 2 retries burned on the throttled VIIRS product.
        assert len(responses.calls) == 5

    @responses.activate
    def test_header_only_response_is_reported_as_empty(self, uploader, tmp_staging) -> None:
        # Both archives empty for the same window: reported as no fires, not a failure.
        responses.add(responses.GET, FIRMS_VIIRS_SP, body="latitude,longitude\n", status=200)
        responses.add(responses.GET, FIRMS_MODIS_SP, body="latitude,longitude\n", status=200)
        responses.add(responses.GET, FIRMS_VIIRS_NRT, body="latitude,longitude\n", status=200)
        responses.add(responses.GET, FIRMS_MODIS_NRT, body="latitude,longitude\n", status=200)
        collector = firms_collector(uploader, tmp_staging)

        summary = collector.run(
            [get_region("iberia_fire")], start_date=date(2024, 8, 15), end_date=date(2024, 8, 15)
        )
        assert summary.results[0].status == "empty"
        assert summary.exit_code() == 0

    @responses.activate
    def test_non_csv_error_body_fails_without_leaking_the_key(
        self, uploader, tmp_staging
    ) -> None:
        responses.add(responses.GET, FIRMS_VIIRS_URL, body=FIRMS_ERROR_TEXT, status=200)
        responses.add(responses.GET, FIRMS_MODIS_URL, body=FIRMS_ERROR_TEXT, status=200)
        collector = firms_collector(uploader, tmp_staging)

        summary = collector.run(
            [get_region("iberia_fire")], start_date=date(2024, 8, 15), end_date=date(2024, 8, 15)
        )
        result = summary.results[0]
        assert result.status == "failed"
        assert result.error_type == "PermanentError"
        assert "non-CSV payload" in (result.error or "")
        assert MAP_KEY not in (result.error or "")
        assert summary.exit_code() == 2

    @responses.activate
    def test_bad_credentials_are_not_retried(self, uploader, tmp_staging) -> None:
        responses.add(responses.GET, FIRMS_VIIRS_URL, body="Invalid MAP_KEY", status=403)
        collector = firms_collector(uploader, tmp_staging)

        collector.run(
            [get_region("iberia_fire")], start_date=date(2024, 8, 15), end_date=date(2024, 8, 15)
        )
        assert len(responses.calls) == 1  # one attempt, no retries
        assert "FIRMS_MAP_KEY" in (collector.run([get_region("greece_fire")]).results[0].error or "")

    @responses.activate
    def test_breaker_short_circuits_remaining_regions(self, uploader, tmp_staging) -> None:
        register_firms_failure(status=500)
        breaker = CircuitBreaker("firms", failure_threshold=1, clock=FakeMonotonic())
        collector = firms_collector(uploader, tmp_staging, breaker=breaker)

        summary = collector.run(
            [get_region("iberia_fire"), get_region("greece_fire")],
            start_date=date(2024, 8, 15),
            end_date=date(2024, 8, 15),
        )

        statuses = {result.region_id: result.status for result in summary.results}
        # One failure trips the breaker, so the in-flight region also stops and the
        # second region is never attempted — instead of 18 hammering requests.
        assert set(statuses.values()) == {"circuit_open"}
        assert len(responses.calls) == 1
        assert summary.exit_code() == 2

    @responses.activate
    def test_multi_instrument_frames_validate_together(self, uploader, tmp_staging) -> None:
        # MODIS lacks bright_ti4/type; VIIRS lacks brightness. One schema must
        # accept both rather than failing whichever lands second.
        register_firms_products()
        collector = firms_collector(uploader, tmp_staging)
        summary = collector.run(
            [get_region("iberia_fire")], start_date=date(2024, 8, 15), end_date=date(2024, 8, 15)
        )
        assert summary.results[0].status == "success"


# --------------------------------------------------------------------------
# NOAA / NSIDC
# --------------------------------------------------------------------------

class TestReadCsvAfterHeader:
    def test_skips_the_nsidc_units_preamble(self) -> None:
        frame = read_csv_after_header(NSIDC_DAILY_CSV, sentinel="Year", units_rows=1)
        assert list(frame.columns)[:4] == ["Year", "Month", "Day", "Extent"]
        assert len(frame) == 4

    def test_units_row_is_kept_when_not_declared(self) -> None:
        # The helper stays faithful to the file; the units row is dropped by the
        # numeric coercion in the collector, not silently by the CSV reader.
        frame = read_csv_after_header(NSIDC_DAILY_CSV, sentinel="Year")
        assert len(frame) == 5

    def test_reads_the_climatology_header(self) -> None:
        frame = read_csv_after_header(NSIDC_CLIMATOLOGY_CSV, sentinel="DOY")
        assert "Average Extent" in frame.columns
        assert len(frame) == 3

    def test_missing_header_fails_loudly(self) -> None:
        with pytest.raises(PermanentError, match="could not find a header"):
            read_csv_after_header("garbage\nmore garbage\n", sentinel="Year")


class TestStrideCalculation:
    def test_one_degree_sampling_on_a_quarter_degree_grid(self) -> None:
        assert _stride_for(1.0) == 4

    def test_zero_sample_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _stride_for(0.0)


class TestNoaaNsidcSeaIce:
    @responses.activate
    def test_sea_ice_window_is_filtered_and_attributed(self, uploader, tmp_staging) -> None:
        responses.add(responses.GET, NSIDC_NORTH_URL, body=NSIDC_DAILY_CSV, status=200)
        collector = nsidc_collector(uploader, tmp_staging)

        summary = collector.run(
            [get_region("arctic")], start_date=date(2024, 9, 13), end_date=date(2024, 9, 15)
        )

        assert summary.exit_code() == 0
        assert summary.results[0].rows == 3  # the 2024-10-01 row is outside the window
        assert summary.results[0].details["partition_count"] == 3

        path = "noaa_nsidc/region=arctic/year=2024/month=09/day=15/noaa_nsidc_arctic_20240915.parquet"
        assert path in uploader.paths
        frame = pd.read_parquet(io.BytesIO(uploader.files[path]))
        assert set(frame["metric_type"]) == {"sea_ice_extent"}
        assert set(frame["unit"]) == {"10^6 km^2"}
        assert set(frame["spatial_scope"]) == {"hemispheric"}
        assert set(frame["region_id"]) == {"arctic"}
        # Live runs partition daily, so each day's partition holds that day's value.
        assert frame["value"].tolist() == [4.601]
        total = sum(len(pd.read_parquet(io.BytesIO(blob))) for blob in uploader.files.values())
        assert total == 3

    @responses.activate
    def test_antarctic_uses_the_southern_series(self, uploader, tmp_staging) -> None:
        responses.add(responses.GET, NSIDC_SOUTH_URL, body=NSIDC_DAILY_CSV, status=200)
        collector = nsidc_collector(uploader, tmp_staging)

        collector.run([get_region("antarctic")], start_date=date(2024, 9, 13), end_date=date(2024, 9, 13))
        assert "south" in responses.calls[0].request.url
        frame = pd.read_parquet(io.BytesIO(next(iter(uploader.files.values()))))
        assert set(frame["region_id"]) == {"antarctic"}

    @responses.activate
    def test_climatology_lands_as_a_reference_baseline(self, uploader, tmp_staging) -> None:
        responses.add(responses.GET, NSIDC_NORTH_URL, body=NSIDC_DAILY_CSV, status=200)
        responses.add(responses.GET, NSIDC_NORTH_URL, body=NSIDC_CLIMATOLOGY_CSV, status=200)
        collector = nsidc_collector(uploader, tmp_staging, include_climatology=True)

        collector.run(
            [get_region("arctic")],
            start_date=date(2024, 9, 13),
            end_date=date(2024, 9, 15),
            backfill=True,
        )

        frames = [pd.read_parquet(io.BytesIO(uploader.files[key])) for key in sorted(uploader.files)]
        combined = pd.concat(frames, ignore_index=True)
        climatology = combined[combined["metric_type"] == "sea_ice_extent_climatology"].sort_values(
            "day_of_year"
        )

        assert len(climatology) == 3
        assert climatology["day_of_year"].tolist() == [1.0, 2.0, 258.0]
        assert climatology["baseline_stddev"].notna().all()
        # Anchored to a non-leap reference year so day-of-year is unambiguous.
        assert set(climatology["timestamp"].dt.year) == {2001}

    @responses.activate
    def test_empty_window_is_not_an_error(self, uploader, tmp_staging) -> None:
        responses.add(responses.GET, NSIDC_NORTH_URL, body=NSIDC_DAILY_CSV, status=200)
        collector = nsidc_collector(uploader, tmp_staging)

        summary = collector.run(
            [get_region("arctic")], start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
        )
        assert summary.results[0].status == "empty"
        assert summary.exit_code() == 0


class TestNoaaOisstBlock:
    def test_flattens_block_with_subsampling_and_masks_fill_values(
        self, uploader, tmp_staging
    ) -> None:
        import numpy as np

        collector = nsidc_collector(uploader, tmp_staging, sample_deg=1.0)
        # Nine 0.25-degree steps; a 1-degree stride must keep three of them.
        lats = np.array([35.5 + 0.25 * step for step in range(9)])
        lons = np.array([350.0 + 0.25 * step for step in range(9)])
        times = pd.to_datetime(["2024-09-13", "2024-09-14"], utc=True).to_numpy()

        values = np.full((2, 9, 9), 0.5)
        values[0, 0, 0] = -9.96921e36  # OISST fill value
        values[1, 8, 8] = 1.25

        frame = collector._block_to_frame(
            values,
            times,
            lats,
            lons,
            missing=-9.96921e36,
            region_id="iberia_fire",
            source_dataset="sst.day.anom.2024.nc",
        )

        assert set(frame["latitude"]) == {35.5, 36.5, 37.5}
        assert set(frame["longitude"]) == {-10.0, -9.0, -8.0}
        assert np.isclose(frame["value"].min(), 0.5)
        assert frame["value"].max() == 1.25
        assert set(frame["metric_type"]) == {"sst_anomaly"}
        assert set(frame["spatial_scope"]) == {"gridded"}
        assert set(frame["region_id"]) == {"iberia_fire"}

    def test_longitudes_are_published_in_minus_180_180(self, uploader, tmp_staging) -> None:
        import numpy as np

        collector = nsidc_collector(uploader, tmp_staging, sample_deg=0.25)
        times = pd.to_datetime(["2024-09-13"], utc=True).to_numpy()
        frame = collector._block_to_frame(
            np.array([[[0.1]]]),
            times,
            np.array([60.0]),
            np.array([359.75]),
            missing=-9.9e36,
            region_id="norway_ice",
            source_dataset="x.nc",
        )
        assert -1.0 < frame["longitude"].iloc[0] <= 0.0


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

class TestPreflight:
    def test_reports_every_missing_secret_at_once(self, monkeypatch) -> None:
        for name in ("FIRMS_MAP_KEY", "GEE_SERVICE_ACCOUNT_JSON", "GEE_PROJECT"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(MissingCredential) as excinfo:
            preflight_sources(["firms", "sentinel"])
        assert "FIRMS_MAP_KEY" in str(excinfo.value)
        assert "GEE_PROJECT" in str(excinfo.value)

    def test_sources_without_credentials_pass(self, monkeypatch) -> None:
        monkeypatch.delenv("HF_TOKEN", raising=False)
        assert preflight_sources(["noaa_nsidc"]) == [{"source_id": "noaa_nsidc"}]

    def test_unknown_source_is_rejected(self) -> None:
        with pytest.raises(KeyError, match="Unknown source"):
            preflight_sources(["nope"])


class TestOisstBlockReader:
    """The axis-layout guard: a wrong rank must fail loudly, not misalign silently."""

    class FakeVariable:
        def __init__(self, values):
            self._values = values
            self.shape = values.shape
            self.attributes = {"missing_value": "-9.96921e+36"}

        def __getitem__(self, selection):
            return type("Slice", (), {"data": self._values[selection]})()

    def test_reads_a_three_dimensional_variable(self, uploader, tmp_staging) -> None:
        import numpy as np


        values = np.arange(4 * 6 * 5, dtype="float64").reshape(4, 6, 5)
        collector: NoaaNsidcCollector = nsidc_collector(uploader, tmp_staging)

        block = collector._read_block(
            self.FakeVariable(values), (0, 2, 1, 5, 2, 4), rank=3
        )
        assert block.shape == (2, 4, 2)
        assert block[0, 0, 0] == values[0, 1, 2]

    def test_reads_a_four_dimensional_variable_with_singleton_depth(
        self, uploader, tmp_staging
    ) -> None:
        import numpy as np

        values = np.arange(4 * 1 * 6 * 5, dtype="float64").reshape(4, 1, 6, 5)
        collector = nsidc_collector(uploader, tmp_staging)

        block = collector._read_block(self.FakeVariable(values), (0, 2, 1, 5, 2, 4), rank=4)
        assert block.shape == (2, 4, 2)
        assert block[0, 0, 0] == values[0, 0, 1, 2]

    def test_misaligned_read_is_rejected(self, uploader, tmp_staging) -> None:
        # This is the real failure that shipped once: a 4-D read applied to a 3-D
        # variable returns fewer values than requested instead of raising.
        import numpy as np

        collector = nsidc_collector(uploader, tmp_staging)
        truncated = np.zeros((4, 1, 34), dtype="float64")

        with pytest.raises(PermanentError, match="axis layout"):
            collector._read_block(self.FakeVariable(truncated), (0, 4, 0, 34, 0, 40), rank=3)

    def test_unsupported_rank_is_rejected(self, uploader, tmp_staging) -> None:
        import numpy as np

        collector = nsidc_collector(uploader, tmp_staging)
        with pytest.raises(PermanentError, match="expected 3"):
            collector._read_block(self.FakeVariable(np.zeros((2, 3))), (0, 2, 0, 3, 0, 1), rank=2)


class TestClimatologyDayOfYear:
    @responses.activate
    def test_leap_day_clamps_to_the_reference_year(self, uploader, tmp_staging) -> None:
        leap_row = "366,            4.100,           0.300,     3.900,     4.000,     4.100,     4.200,     4.300\n"
        responses.add(
            responses.GET,
            NSIDC_NORTH_URL,
            body=NSIDC_DAILY_CSV,
            status=200,
        )
        responses.add(
            responses.GET,
            NSIDC_NORTH_URL,
            body=NSIDC_CLIMATOLOGY_CSV + leap_row,
            status=200,
        )
        collector = nsidc_collector(uploader, tmp_staging, include_climatology=True)

        collector.run(
            [get_region("arctic")],
            start_date=date(2024, 9, 13),
            end_date=date(2024, 9, 13),
            backfill=True,
        )

        frames = [
            pd.read_parquet(io.BytesIO(uploader.files[key])) for key in sorted(uploader.files)
        ]
        combined = pd.concat(frames, ignore_index=True)
        leap = combined[combined["day_of_year"] == 366.0]
        assert len(leap) == 1
        # Must stay inside the reference year rather than spilling into next January.
        assert leap["timestamp"].dt.year.tolist() == [2001]
        assert leap["timestamp"].dt.strftime("%Y-%m-%d").tolist() == ["2001-12-31"]


class TestProductPlan:
    """Archive selection, measured against the live API rather than assumed."""

    PAIRS = (
        ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
        ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT"),
        ("MODIS_SP", "MODIS_NRT"),
    )
    TODAY = date(2026, 9, 21)

    def test_old_window_uses_standard_processing_only(self) -> None:
        preferred, alternative = product_plan(self.PAIRS, date(2024, 1, 1), self.TODAY)
        assert preferred == ("VIIRS_SNPP_SP", "VIIRS_NOAA20_SP", "MODIS_SP")
        assert alternative == ()

    def test_recent_window_uses_near_real_time_only(self) -> None:
        # 11 days old: SP is roughly three months behind, so it cannot exist yet.
        preferred, alternative = product_plan(self.PAIRS, date(2026, 9, 10), self.TODAY)
        assert preferred == ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT")
        assert alternative == ()

    def test_boundary_window_offers_both_archives(self) -> None:
        # ~105 days old: SP should be processed and NRT is still retained, so the
        # fallback closes any error in the measured boundary.
        preferred, alternative = product_plan(self.PAIRS, date(2026, 6, 8), self.TODAY)
        assert all(product.endswith("_SP") for product in preferred)
        assert all(product.endswith("_NRT") for product in alternative)

    def test_instruments_are_paired_not_mixed(self) -> None:
        preferred, alternative = product_plan(
            (("A_SP", "A_NRT"), ("B_SP", "B_NRT")), date(2020, 1, 1), self.TODAY
        )
        assert preferred == ("A_SP", "B_SP")
        assert alternative == ()

    def test_window_covered_by_neither_archive_is_reported(self) -> None:
        # 120 days old: past NRT retention, before SP is published.
        preferred, alternative = product_plan(
            self.PAIRS,
            self.TODAY - timedelta(days=120),
            self.TODAY,
            sp_min_age_days=150,
            nrt_max_age_days=100,
        )
        assert preferred == ()
        assert alternative == ()

    def test_exactly_at_each_boundary_is_covered(self) -> None:
        at_sp = product_plan(self.PAIRS, self.TODAY - timedelta(days=100), self.TODAY)
        assert at_sp[0], "SP should be available exactly at its minimum age"
        at_nrt = product_plan(self.PAIRS, self.TODAY - timedelta(days=112), self.TODAY)
        assert at_nrt[1], "NRT should still be retained exactly at its maximum age"


class TestFirmsTransientClientErrors:
    """FIRMS uses HTTP 400 for transient conditions, so it must be retried."""

    def test_firms_declares_400_as_transient(self) -> None:
        assert 400 in FirmsCollector.transient_client_errors

    def test_other_sources_keep_the_strict_rule(self) -> None:
        from collectors.noaa_nsidc_collector import NoaaNsidcCollector

        assert NoaaNsidcCollector.transient_client_errors == ()

    @responses.activate
    def test_intermittent_invalid_map_key_is_retried(self, uploader, tmp_staging) -> None:
        # Observed mid-backfill: the same key works either side of the failure.
        responses.add(responses.GET, FIRMS_VIIRS_URL, body="Invalid MAP_KEY.", status=400)
        responses.add(responses.GET, FIRMS_VIIRS_URL, body=FIRMS_VIIRS_CSV, status=200)
        responses.add(responses.GET, FIRMS_MODIS_URL, body=FIRMS_MODIS_CSV, status=200)
        collector = firms_collector(uploader, tmp_staging)

        summary = collector.run(
            [get_region("iberia_fire")], start_date=date(2024, 8, 5), end_date=date(2024, 8, 9)
        )

        assert summary.exit_code() == 0
        assert summary.total_rows == 9


class TestPartialRunStillUploads:
    """A failed region must not discard the data that did arrive."""

    @responses.activate
    def test_deferred_backfill_upload_happens_despite_a_failed_region(
        self, uploader, tmp_staging
    ) -> None:
        # Iberia succeeds, Greece exhausts its retries.
        responses.add(responses.GET, FIRMS_VIIRS_URL, body=FIRMS_VIIRS_CSV, status=200)
        responses.add(responses.GET, FIRMS_MODIS_URL, body=FIRMS_MODIS_CSV, status=200)
        collector = firms_collector(
            uploader,
            tmp_staging,
            policy=RetryPolicy(attempts=2, base_delay=0.01, jitter="none"),
        )
        regions = [get_region("iberia_fire"), get_region("greece_fire")]

        # Greece is made to fail by removing its responses after the first region.
        original = collector._download

        def fail_for_greece(product, region, start, days):
            if region.region_id == "greece_fire":
                raise PermanentError("simulated upstream rejection")
            return original(product, region, start, days)

        collector._download = fail_for_greece  # type: ignore[method-assign]

        summary = collector.run(regions, start_date=date(2024, 1, 1), end_date=date(2024, 1, 5), backfill=True)

        assert len(summary.failed) == 1
        assert summary.failed[0].region_id == "greece_fire"
        # The successful region's rows still reached the Hub.
        assert uploader.folders, "partial data should still be uploaded"
        assert any("iberia_fire" in path for path in uploader.paths)
