"""The Pandera contracts, and the specificity of their failure messages.

Validator tests matter more than they look: the whole point of validating at
ingestion is that a failure tells you *which column* broke and *what value* did it,
in the layer where the data arrived — not three transformations later.
"""

from __future__ import annotations

import io
import random

import pandas as pd
import pytest
from pandera.errors import SchemaErrors

from collectors.config import REGIONS, SOURCES, Region
from collectors.firms_collector import FirmsCollector
from ops.resilience import SchemaValidationError
from pandera_schemas import (
    BRONZE_ENVELOPE,
    BRONZE_FIRMS,
    BRONZE_NOAA_NSIDC,
    BRONZE_SENTINEL,
    METRIC_UNITS,
    SILVER_OBSERVATIONS,
    validate_frame,
)
from tests.conftest import FIRMS_VIIRS_CSV, RecordingSleep, StepClock, utc


def col_all_null(frame, column: str) -> bool:
    return bool(frame[column].isna().all())


def envelope_frame(**overrides):
    base = {
        "source_id": "firms",
        "metric_type": "fire_radiative_power",
        "region_id": "iberia_fire",
        "h3_index": "873906108ffffff",
        "ingested_at": pd.Timestamp("2024-08-17T06:00:00", tz="UTC"),
        "run_id": "run-20240817-abc123",
    }
    base.update(overrides)
    return pd.DataFrame([base])


class TestBronzeEnvelope:
    def test_accepts_a_well_formed_row(self) -> None:
        validated = validate_frame(envelope_frame(), BRONZE_ENVELOPE)
        assert len(validated) == 1

    def test_accepts_a_regionless_row(self) -> None:
        assert len(validate_frame(envelope_frame(region_id=None), BRONZE_ENVELOPE)) == 1

    def test_rejects_a_bogus_h3_index(self) -> None:
        with pytest.raises(SchemaValidationError) as excinfo:
            validate_frame(envelope_frame(h3_index="not-a-cell"), BRONZE_ENVELOPE)
        assert "h3_index" in str(excinfo.value)
        assert "not-a-cell" in str(excinfo.value)

    def test_rejects_a_short_run_id(self) -> None:
        with pytest.raises(SchemaValidationError, match="run_id"):
            validate_frame(envelope_frame(run_id="abc"), BRONZE_ENVELOPE)

    def test_coerces_a_naive_timestamp_to_utc(self) -> None:
        # Documented behaviour: the envelope coerces to UTC rather than rejecting,
        # because every timestamp in this pipeline is UTC by construction. The
        # guard against a naive *source* clock lives in BaseCollector.enrich.
        validated = validate_frame(
            envelope_frame(ingested_at=pd.Timestamp("2024-08-17T06:00:00")), BRONZE_ENVELOPE
        )
        assert str(validated["ingested_at"].dtype) == "datetime64[ns, UTC]"

    def test_rejects_a_missing_provenance_column(self) -> None:
        frame = envelope_frame().drop(columns=["source_id"])
        with pytest.raises(SchemaValidationError, match="source_id"):
            validate_frame(frame, BRONZE_ENVELOPE)


class TestBronzeFirms:
    @pytest.fixture
    def normalised(self) -> pd.DataFrame:
        collector = FirmsCollector(
            map_key="k" * 34,
            sleep=RecordingSleep(),
            rng=random.Random(3),
            now=StepClock(utc(2024, 8, 17)),
            run_id="run-schema-test",
        )
        raw = pd.read_csv(io.StringIO(FIRMS_VIIRS_CSV), dtype=str, skipinitialspace=True)
        raw["product"] = "VIIRS_SNPP_NRT"
        return collector.normalize(raw, Region("iberia_fire", "Iberia", "fire", (-10.0, 35.5, 3.5, 43.9)))

    def test_accepts_real_shaped_viirs_rows(self, normalised: pd.DataFrame) -> None:
        assert len(validate_frame(normalised, BRONZE_FIRMS)) == 4

    def test_rejects_impossible_latitude(self, normalised: pd.DataFrame) -> None:
        normalised.loc[0, "latitude"] = 120.0
        with pytest.raises(SchemaValidationError, match="latitude"):
            validate_frame(normalised, BRONZE_FIRMS)

    def test_rejects_an_unknown_confidence_level(self, normalised: pd.DataFrame) -> None:
        normalised.loc[0, "confidence"] = "probably"
        with pytest.raises(SchemaValidationError, match="confidence"):
            validate_frame(normalised, BRONZE_FIRMS)

    def test_rejects_a_negative_fire_radiative_power(self, normalised: pd.DataFrame) -> None:
        normalised.loc[0, "frp"] = -1.0
        with pytest.raises(SchemaValidationError, match="frp"):
            validate_frame(normalised, BRONZE_FIRMS)

    def test_accepts_optional_columns_being_absent(self, normalised: pd.DataFrame) -> None:
        # MODIS rows carry brightness and no bright_ti4/type; both must validate.
        frame = normalised.drop(columns=["bright_ti4", "type"])
        assert len(validate_frame(frame, BRONZE_FIRMS)) == 4


class TestBronzeSentinel:
    def test_accepts_region_grain_row(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "window_start": pd.Timestamp("2024-08-01", tz="UTC"),
                    "window_end": pd.Timestamp("2024-08-31", tz="UTC"),
                    "latitude": 45.6,
                    "longitude": 24.5,
                    "product": "S2_SR_HARMONIZED",
                    "metric_type": "ndvi",
                    "mean": 0.72,
                    "stddev": 0.08,
                    "min": 0.4,
                    "max": 0.9,
                    "observation_count": 1200,
                    "spatial_scope": "region",
                }
            ]
        )
        assert len(validate_frame(frame, BRONZE_SENTINEL)) == 1

    def test_rejects_an_unknown_product(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "window_start": pd.Timestamp("2024-08-01", tz="UTC"),
                    "window_end": pd.Timestamp("2024-08-31", tz="UTC"),
                    "latitude": 45.6,
                    "longitude": 24.5,
                    "product": "LANDSAT",
                    "metric_type": "ndvi",
                }
            ]
        )
        with pytest.raises(SchemaValidationError, match="product"):
            validate_frame(frame, BRONZE_SENTINEL)


class TestBronzeNoaaNsidc:
    def test_accepts_a_hemispheric_row(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2024-09-15", tz="UTC"),
                    "latitude": 75.0,
                    "longitude": 0.0,
                    "metric_type": "sea_ice_extent",
                    "product": "nsidc_seaice_index_v4_0",
                    "value": 4.601,
                    "unit": "10^6 km^2",
                    "spatial_scope": "hemispheric",
                }
            ]
        )
        assert len(validate_frame(frame, BRONZE_NOAA_NSIDC)) == 1

    def test_rejects_an_unknown_metric(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2024-09-15", tz="UTC"),
                    "latitude": 75.0,
                    "longitude": 0.0,
                    "metric_type": "salinity",
                    "product": "x",
                    "value": 1.0,
                }
            ]
        )
        with pytest.raises(SchemaValidationError, match="metric_type"):
            validate_frame(frame, BRONZE_NOAA_NSIDC)

    def test_rejects_an_impossible_day_of_year(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2001-01-01", tz="UTC"),
                    "latitude": 75.0,
                    "longitude": 0.0,
                    "metric_type": "sea_ice_extent_climatology",
                    "product": "nsidc",
                    "value": 13.7,
                    "day_of_year": 400.0,
                }
            ]
        )
        with pytest.raises(SchemaValidationError, match="day_of_year"):
            validate_frame(frame, BRONZE_NOAA_NSIDC)


class TestSilverObservations:
    def test_accepts_a_unified_observation(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "observed_at": pd.Timestamp("2024-08-15T13:20:00", tz="UTC"),
                    "metric_type": "fire_radiative_power",
                    "value": 12.5,
                    "unit": "MW",
                    "source_id": "firms",
                    "region_id": "iberia_fire",
                    "h3_index": "873906108ffffff",
                    "latitude": 39.45,
                    "longitude": -8.12,
                    "product": "VIIRS_SNPP_NRT",
                    "ingested_at": pd.Timestamp("2024-08-17T06:00:00", tz="UTC"),
                }
            ]
        )
        assert len(validate_frame(frame, SILVER_OBSERVATIONS)) == 1

    def test_rejects_an_unknown_metric(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "observed_at": pd.Timestamp("2024-08-15", tz="UTC"),
                    "metric_type": "vibes",
                    "value": 1.0,
                    "unit": "MW",
                    "source_id": "firms",
                    "region_id": "iberia_fire",
                    "h3_index": "873906108ffffff",
                    "latitude": 39.45,
                    "longitude": -8.12,
                    "product": "x",
                    "ingested_at": pd.Timestamp("2024-08-15", tz="UTC"),
                }
            ]
        )
        with pytest.raises(SchemaValidationError, match="metric_type"):
            validate_frame(frame, SILVER_OBSERVATIONS)


class TestContractCoverage:
    def test_every_declared_source_metric_has_a_unit(self) -> None:
        # A metric with no unit would silently produce null units in silver.
        for spec in SOURCES.values():
            for metric in spec.metric_types:
                assert metric in METRIC_UNITS, f"{spec.source_id}: {metric} has no declared unit"

    @pytest.mark.parametrize("source_id", sorted(SOURCES))
    def test_silver_schema_accepts_every_source(self, source_id: str) -> None:
        frame = pd.DataFrame(
            [
                {
                    "observed_at": pd.Timestamp("2024-08-15T13:20:00", tz="UTC"),
                    "metric_type": SOURCES[source_id].metric_types[0],
                    "value": 1.0,
                    "unit": METRIC_UNITS[SOURCES[source_id].metric_types[0]],
                    "source_id": source_id,
                    "region_id": "iberia_fire",
                    "h3_index": "873906108ffffff",
                    "latitude": 39.45,
                    "longitude": -8.12,
                    "product": "test",
                    "ingested_at": pd.Timestamp("2024-08-17T06:00:00", tz="UTC"),
                }
            ]
        )
        assert len(validate_frame(frame, SILVER_OBSERVATIONS)) == 1

    def test_region_ids_are_unique(self) -> None:
        ids = [region.region_id for region in REGIONS]
        assert len(ids) == len(set(ids))

    def test_every_source_has_attribution_text(self) -> None:
        # The dashboard footer is generated from these; a missing string would
        # breach the providers' open-data terms.
        for spec in SOURCES.values():
            assert spec.attribution.strip()
            assert spec.attribution.endswith(".")

    def test_validate_frame_reports_multiple_failures(self) -> None:
        frame = envelope_frame(h3_index="bad", run_id="x")
        with pytest.raises(SchemaValidationError) as excinfo:
            validate_frame(frame, BRONZE_ENVELOPE)
        message = str(excinfo.value)
        assert "h3_index" in message
        assert "run_id" in message

    def test_schema_errors_are_wrapped_consistently(self) -> None:
        with pytest.raises(SchemaValidationError):
            validate_frame(pd.DataFrame({"nonsense": [1]}), BRONZE_ENVELOPE)
        assert issubclass(SchemaValidationError.__mro__[1], Exception)
        assert SchemaErrors is not None


class TestOptionalColumnTolerance:
    """A declared-but-absent optional column must validate when null-filled.

    `conform_to_schema` materialises absent optional columns so downstream SQL can
    reference them unconditionally. That only works if the schema accepts an
    all-null column of the declared type — which is not automatic, and has broken
    three separate times (object dtype, then numpy int64, which cannot hold NA).
    """

    @pytest.mark.parametrize(
        "column,frame_overrides",
        [
            ("observation_count", {"metric_type": "ndvi"}),
            ("cloud_pct", {"metric_type": "ndvi"}),
            ("stddev", {"metric_type": "sar_backscatter"}),
            ("baseline_mean", {"metric_type": "sar_backscatter"}),
            ("recent_mean", {"metric_type": "sar_backscatter"}),
        ],
    )
    def test_sentinel_optional_columns_can_be_all_null(
        self, column: str, frame_overrides: dict
    ) -> None:
        import pandas as pd

        from pandera_schemas import BRONZE_SENTINEL, conform_to_schema

        frame = pd.DataFrame(
            [
                {
                    "window_start": pd.Timestamp("2024-08-01", tz="UTC"),
                    "window_end": pd.Timestamp("2024-08-31", tz="UTC"),
                    "latitude": 45.6,
                    "longitude": 24.5,
                    "product": "S2_SR_HARMONIZED",
                    "metric_type": frame_overrides["metric_type"],
                    "spatial_scope": "grid",
                    "mean": -0.2,
                }
            ]
        )
        conformed = conform_to_schema(frame, BRONZE_SENTINEL)
        assert column in conformed.columns
        assert col_all_null(conformed, column)
        # The whole point: null-filling must not make the frame invalid.
        assert len(validate_frame(conformed, BRONZE_SENTINEL)) == 1

    def test_firms_optional_columns_can_be_all_null(self) -> None:
        import pandas as pd

        from pandera_schemas import BRONZE_FIRMS, conform_to_schema

        frame = pd.DataFrame(
            [
                {
                    "latitude": 39.45,
                    "longitude": -8.12,
                    "acq_datetime": pd.Timestamp("2024-08-15T13:20:00", tz="UTC"),
                    "acq_date": "2024-08-15",
                    "acq_time": "1320",
                    "product": "VIIRS_SNPP_NRT",
                    "confidence": "nominal",
                    "confidence_pct": 60.0,
                }
            ]
        )
        conformed = conform_to_schema(frame, BRONZE_FIRMS)
        for column in ("brightness", "bright_ti4", "bright_ti5", "frp", "scan", "track", "type"):
            assert col_all_null(conformed, column), column
        assert len(validate_frame(conformed, BRONZE_FIRMS)) == 1

    def test_noaa_optional_columns_can_be_all_null(self) -> None:
        import pandas as pd

        from pandera_schemas import BRONZE_NOAA_NSIDC, conform_to_schema

        frame = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2024-09-15", tz="UTC"),
                    "latitude": 75.0,
                    "longitude": 0.0,
                    "metric_type": "sea_ice_extent",
                    "product": "nsidc_seaice_index_v4_0",
                    "value": 4.601,
                    "unit": "10^6 km^2",
                }
            ]
        )
        conformed = conform_to_schema(frame, BRONZE_NOAA_NSIDC)
        for column in ("anomaly", "baseline_stddev", "day_of_year", "spatial_scope"):
            assert col_all_null(conformed, column), column
        assert len(validate_frame(conformed, BRONZE_NOAA_NSIDC)) == 1

    def test_conformance_preserves_undeclared_columns(self) -> None:
        import pandas as pd

        from pandera_schemas import BRONZE_NOAA_NSIDC, conform_to_schema

        frame = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2024-09-15", tz="UTC"),
                    "latitude": 75.0,
                    "longitude": 0.0,
                    "metric_type": "sea_ice_extent",
                    "product": "nsidc_seaice_index_v4_0",
                    "value": 4.601,
                    "unit": "10^6 km^2",
                    "source_dataset": "N_seaice_extent_daily_v4.0.csv",
                }
            ]
        )
        conformed = conform_to_schema(frame, BRONZE_NOAA_NSIDC)
        # Bronze is append-only and source-shaped: a new upstream field must not be
        # dropped just because the contract predates it.
        assert "source_dataset" in conformed.columns
