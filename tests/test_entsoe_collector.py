"""ENTSO-E collector: registry contract, zone mapping, document parsing, pacing.

The XML fixtures mirror live responses shape-for-shape: A44 ``price.amount``
values in ``EUR/MWH`` across two Periods with a sparse position, A65
``quantity`` values labelled ``MAW``, and the `Acknowledgement_MarketDocument`
the platform returns for an empty window. HTTP is mocked at the ``requests``
layer, so the real retry, backoff, pacing and redaction code is exercised
rather than stubbed out.
"""

from __future__ import annotations

import random
import re
from datetime import date
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import responses

from collectors.backfill_historical import COLLECTOR_CLASSES, estimate_requests
from collectors.config import REGIONS, SOURCES, get_region
from collectors.entsoe_collector import (
    DOCUMENTS,
    ENTSOE_ENDPOINT,
    REQUEST_INTERVAL_S,
    ZONE_BY_REGION,
    EntsoeCollector,
    _parse_document,
    _resolution_to_timedelta,
    iter_month_chunks,
)
from ops.redact import (
    REDACTED,
    redact_text,
    redact_url,
    register_secret,
    secret_values_from_env,
)
from ops.resilience import PermanentError, RetryPolicy, SchemaValidationError, TransientError
from pandera_schemas import BRONZE_ENTSOE, METRIC_UNITS, conform_to_schema, validate_frame
from tests.conftest import RecordingSleep, StepClock, utc

TOKEN = "entsoe-token-0000-1111-2222"

#: Query-scoped matchers: one endpoint answers both documents, so the
#: ``documentType`` parameter is what routes each call to the right fixture.
PRICE_URL = re.compile(r"https://web-api\.tp\.entsoe\.eu/api.*documentType=A44.*")
LOAD_URL = re.compile(r"https://web-api\.tp\.entsoe\.eu/api.*documentType=A65.*")

#: A44 day-ahead price, two Periods (market days start 22:00Z), positions
#: restarting at 1 per Period, and one genuinely absent position (2) that must
#: NOT be back-filled with an invented value.
PRICE_DOC = """<?xml version="1.0" encoding="utf-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <mRID>a11febaf5f1f40e5bec1f736dbed2a92</mRID>
  <revisionNumber>1</revisionNumber>
  <type>A44</type>
  <period.timeInterval>
    <start>2026-09-19T22:00Z</start>
    <end>2026-09-21T22:00Z</end>
  </period.timeInterval>
  <TimeSeries>
    <mRID>1</mRID>
    <businessType>A62</businessType>
    <in_Domain.mRID codingScheme="A01">10YGR-HTSO-----Y</in_Domain.mRID>
    <out_Domain.mRID codingScheme="A01">10YGR-HTSO-----Y</out_Domain.mRID>
    <currency_Unit.name>EUR</currency_Unit.name>
    <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <curveType>A03</curveType>
    <Period>
      <timeInterval>
        <start>2026-09-19T22:00Z</start>
        <end>2026-09-20T22:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point>
        <position>1</position>
        <price.amount>213.7</price.amount>
      </Point>
      <Point>
        <position>3</position>
        <price.amount>211.7</price.amount>
      </Point>
      <Point>
        <position>4</position>
        <price.amount>210.7</price.amount>
      </Point>
    </Period>
    <Period>
      <timeInterval>
        <start>2026-09-20T22:00Z</start>
        <end>2026-09-21T22:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point>
        <position>1</position>
        <price.amount>181.74</price.amount>
      </Point>
      <Point>
        <position>96</position>
        <price.amount>179.0</price.amount>
      </Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""

#: A65 actual total load: one Period, quantity values labelled MAW.
LOAD_DOC = """<?xml version="1.0" encoding="utf-8"?>
<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">
  <mRID>843a47eec8d546848d8e9dcbd1b2aaf7</mRID>
  <revisionNumber>1</revisionNumber>
  <type>A65</type>
  <period.timeInterval>
    <start>2026-09-20T00:00Z</start>
    <end>2026-09-21T00:00Z</end>
  </period.timeInterval>
  <TimeSeries>
    <mRID>1</mRID>
    <businessType>A04</businessType>
    <outBiddingZone_Domain.mRID codingScheme="A01">10YGR-HTSO-----Y</outBiddingZone_Domain.mRID>
    <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
    <curveType>A03</curveType>
    <Period>
      <timeInterval>
        <start>2026-09-20T00:00Z</start>
        <end>2026-09-21T00:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point>
        <position>1</position>
        <quantity>4386</quantity>
      </Point>
      <Point>
        <position>2</position>
        <quantity>4333</quantity>
      </Point>
      <Point>
        <position>3</position>
        <quantity>4393.5</quantity>
      </Point>
    </Period>
  </TimeSeries>
</GL_MarketDocument>
"""

#: What an empty window looks like: an acknowledgement, not an error document.
ACK_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
  <mRID>47f7109d-52ed-4</mRID>
  <createdDateTime>2026-09-22T12:12:24Z</createdDateTime>
  <reason>No matching data found for Data item ACTUAL_TOTAL_LOAD_R3 [6.1.A] (10YGR-HTSO-----Y) and interval 2030-01-01T00:00:00Z/2030-01-02T00:00:00Z.</reason>
</Acknowledgement_MarketDocument>
"""


def entsoe_collector(uploader, staging_dir, **overrides):
    defaults = dict(
        uploader=uploader,
        staging_dir=staging_dir,
        sleep=RecordingSleep(),
        rng=random.Random(7),
        now=StepClock(utc(2026, 9, 21, 8, 0), utc(2026, 9, 21, 8, 0, 12)),
        run_id="run-entsoe-test01",
        policy=RetryPolicy(attempts=2, base_delay=0.01, jitter="none"),
    )
    defaults.update(overrides)
    return EntsoeCollector(**defaults)


def _register_documents(*, price: str = PRICE_DOC, load: str = LOAD_DOC) -> None:
    responses.add(responses.GET, PRICE_URL, body=price, status=200)
    responses.add(responses.GET, LOAD_URL, body=load, status=200)


def _query_of(call) -> dict[str, list[str]]:
    return parse_qs(urlparse(call.request.url).query)


class TestRegistryContract:
    def test_source_is_registered_with_the_daily_cadence(self) -> None:
        spec = SOURCES["entsoe"]
        assert spec.cadence_cron == "35 7 * * *"
        assert spec.cadence_human == "daily"
        assert spec.required_secrets == ("ENTSOE_API_KEY",)
        assert spec.metric_types == ("day_ahead_price", "actual_load")
        assert spec.anomaly_types == ("fire", "deforestation", "ice")
        assert spec.h3_resolution == 7
        assert spec.live_products == spec.backfill_products

    def test_every_declared_metric_has_a_canonical_unit(self) -> None:
        for metric in SOURCES["entsoe"].metric_types:
            assert metric in METRIC_UNITS
        assert METRIC_UNITS["day_ahead_price"] == "EUR/MWh"
        assert METRIC_UNITS["actual_load"] == "MW"

    def test_collector_is_wired_into_the_backfill_registry(self) -> None:
        assert "entsoe" in COLLECTOR_CLASSES
        assert COLLECTOR_CLASSES["entsoe"] is EntsoeCollector

    def test_class_defaults_follow_the_multi_metric_sentinel_convention(self) -> None:
        # Rows carry their own metric_type (price vs load), so a class-wide
        # default would lie for half the rows — sentinel sets "" for the same
        # reason (ndvi vs sar_backscatter).
        assert EntsoeCollector.metric_type == ""
        assert EntsoeCollector.raw_schema is BRONZE_ENTSOE
        assert EntsoeCollector.time_column == "timestamp"
        assert EntsoeCollector.default_region_ids == tuple(ZONE_BY_REGION)


class TestZoneMapping:
    def test_zone_codes_are_the_verified_area_codes(self) -> None:
        assert ZONE_BY_REGION == {
            "greece_fire": ("10YGR-HTSO-----Y",),
            "iberia_fire": ("10YES-REE------0", "10YPT-REN------W"),
            "carpathian_deforest": ("10YRO-TEL------P",),
            "alps_ice": ("10YAT-APG------L",),
            "norway_ice": ("10YNO-0--------C",),
        }

    def test_every_mapped_region_exists(self) -> None:
        region_ids = {region.region_id for region in REGIONS}
        assert set(ZONE_BY_REGION) <= region_ids

    def test_iberia_carries_both_peninsula_zones(self) -> None:
        # Two zones, one region: the zone_code column is what keeps the ES and
        # PT series apart downstream.
        iberia = ZONE_BY_REGION["iberia_fire"]
        assert len(iberia) == 2
        assert len(set(iberia)) == 2

    @pytest.mark.parametrize("region_id", ["arctic", "antarctic"])
    def test_unmapped_regions_fetch_empty_without_credentials(
        self, region_id: str, uploader, tmp_staging, monkeypatch
    ) -> None:
        # The zone check must run *before* get_secret: a region with no bidding
        # zone is a no-op, not a credential failure.
        monkeypatch.delenv("ENTSOE_API_KEY", raising=False)
        collector = entsoe_collector(uploader, tmp_staging)
        frame = collector.fetch(get_region(region_id), start_date=date(2026, 9, 20), end_date=date(2026, 9, 21))
        assert frame.empty


class TestResolutionParsing:
    @pytest.mark.parametrize(
        ("resolution", "minutes"),
        [
            ("PT15M", 15),
            ("PT30M", 30),
            ("PT60M", 60),
            ("PT1H", 60),
            ("P1D", 24 * 60),
        ],
    )
    def test_iso_durations_resolve_to_timedeltas(self, resolution: str, minutes: int) -> None:
        assert _resolution_to_timedelta(resolution).total_seconds() == minutes * 60

    @pytest.mark.parametrize("resolution", ["PT", "P0D", "15M", "banana", ""])
    def test_unsupported_resolutions_fail_loudly(self, resolution: str) -> None:
        with pytest.raises(PermanentError, match="resolution"):
            _resolution_to_timedelta(resolution)


class TestDocumentParsing:
    def test_price_document_restarts_positions_per_period(self) -> None:
        points, unit = _parse_document(PRICE_DOC, document=DOCUMENTS[0])
        assert unit == "EUR/MWH"
        values = [value for _, value in points]
        stamps = [stamp for stamp, _ in points]
        # Period 1: the absent position 2 (22:15) is skipped, never invented.
        assert values[:3] == [213.7, 211.7, 210.7]
        assert str(stamps[0].tz_convert("UTC")) == "2026-09-19 22:00:00+00:00"
        assert str(stamps[1].tz_convert("UTC")) == "2026-09-19 22:30:00+00:00"
        assert str(stamps[2].tz_convert("UTC")) == "2026-09-19 22:45:00+00:00"
        # Period 2: positions restart at 1 against *its own* start.
        assert str(stamps[3].tz_convert("UTC")) == "2026-09-20 22:00:00+00:00"
        # Position 96 is the last quarter-hour of the delivery day.
        assert str(stamps[4].tz_convert("UTC")) == "2026-09-21 21:45:00+00:00"
        assert values[3:] == [181.74, 179.0]

    def test_load_document_parses_quantity_with_verbatim_unit(self) -> None:
        points, unit = _parse_document(LOAD_DOC, document=DOCUMENTS[1])
        assert unit == "MAW"
        assert [value for _, value in points] == [4386.0, 4333.0, 4393.5]
        stamps = [stamp for stamp, _ in points]
        assert str(stamps[1].tz_convert("UTC")) == "2026-09-20 00:15:00+00:00"

    @pytest.mark.parametrize("index", [0, 1])
    def test_acknowledgement_is_an_empty_window_not_an_error(self, index: int) -> None:
        points, unit = _parse_document(ACK_DOC, document=DOCUMENTS[index])
        assert points == []
        assert unit is None

    def test_a_genuine_error_document_fails_loudly(self) -> None:
        error_doc = (
            "<Acknowledgement_MarketDocument xmlns=\"urn:x\">"
            "<reason>Invalid access token provided</reason>"
            "</Acknowledgement_MarketDocument>"
        )
        with pytest.raises(PermanentError, match="Invalid access token"):
            _parse_document(error_doc, document=DOCUMENTS[1])

    def test_renamed_value_element_fails_loudly(self) -> None:
        # The platform has shipped both `Price.amount` and `price.amount`; if
        # it renames the element again, points-without-values must not silently
        # hollow out the series.
        drifted = PRICE_DOC.replace("<price.amount>", "<Price.amount>").replace(
            "</price.amount>", "</Price.amount>"
        )
        with pytest.raises(PermanentError, match="price.amount"):
            _parse_document(drifted, document=DOCUMENTS[0])

    def test_unparseable_body_fails_loudly(self) -> None:
        with pytest.raises(PermanentError, match="unparseable"):
            _parse_document("<not-really-xml", document=DOCUMENTS[1])

    def test_positions_must_be_one_based(self) -> None:
        broken = PRICE_DOC.replace(">1</position>", ">0</position>")
        with pytest.raises(PermanentError, match="not 1-based"):
            _parse_document(broken, document=DOCUMENTS[0])


class TestMonthChunks:
    def test_chunks_cut_on_month_boundaries_and_cover_the_inclusive_end(self) -> None:
        chunks = list(iter_month_chunks(date(2026, 1, 15), date(2026, 4, 2)))
        assert chunks[0] == (date(2026, 1, 15), date(2026, 2, 1))
        assert all(left[1] == right[0] for left, right in zip(chunks, chunks[1:], strict=False))
        # The CLI's end date is inclusive; periodEnd is exclusive.
        assert chunks[-1] == (date(2026, 4, 1), date(2026, 4, 3))

    def test_window_inside_one_month_is_a_single_chunk(self) -> None:
        assert list(iter_month_chunks(date(2026, 2, 10), date(2026, 2, 27))) == [
            (date(2026, 2, 10), date(2026, 2, 28))
        ]

    def test_year_boundary_splits_correctly(self) -> None:
        chunks = list(iter_month_chunks(date(2026, 12, 15), date(2027, 1, 5)))
        assert chunks == [
            (date(2026, 12, 15), date(2027, 1, 1)),
            (date(2027, 1, 1), date(2027, 1, 6)),
        ]


class TestFetch:
    @responses.activate
    def test_rows_carry_the_region_anchor_zone_and_scope(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        _register_documents()
        collector = entsoe_collector(uploader, tmp_staging)
        frame = collector.fetch(
            get_region("greece_fire"), start_date=date(2026, 9, 20), end_date=date(2026, 9, 21)
        )

        # 5 price points + 3 load points, one chunk, one zone.
        assert len(frame) == 8
        greece = get_region("greece_fire")
        centre_lat = (greece.bbox[1] + greece.bbox[3]) / 2.0
        centre_lon = (greece.bbox[0] + greece.bbox[2]) / 2.0
        assert set(frame["latitude"]) == {centre_lat}
        assert set(frame["longitude"]) == {centre_lon}
        assert set(frame["zone_code"]) == {"10YGR-HTSO-----Y"}
        assert set(frame["spatial_scope"]) == {"bidding_zone"}
        assert set(frame["region_id"]) == {"greece_fire"}
        assert set(frame["metric_type"]) == {"day_ahead_price", "actual_load"}
        assert set(frame["product"]) == {"A44_day_ahead_price", "A65_actual_load"}
        assert set(frame["unit"]) == {"EUR/MWH", "MAW"}
        assert 213.7 in set(frame["value"])
        assert 4393.5 in set(frame["value"])
        validate_frame(frame, BRONZE_ENTSOE)

    @responses.activate
    def test_request_parameters_follow_the_documented_shape(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        _register_documents()
        collector = entsoe_collector(uploader, tmp_staging)
        collector.fetch(
            get_region("greece_fire"), start_date=date(2026, 9, 20), end_date=date(2026, 9, 21)
        )

        urls = [call.request.url for call in responses.calls]
        assert len(urls) == 2
        price_call = next(call for call in responses.calls if "documentType=A44" in call.request.url)
        load_call = next(call for call in responses.calls if "documentType=A65" in call.request.url)

        parsed = urlparse(price_call.request.url)
        assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "web-api.tp.entsoe.eu", "/api")
        price = _query_of(price_call)
        assert price["documentType"] == ["A44"]
        assert price["processType"] == ["A01"]
        assert price["In_Domain"] == ["10YGR-HTSO-----Y"]
        assert price["Out_Domain"] == ["10YGR-HTSO-----Y"]
        assert price["periodStart"] == ["202609200000"]
        assert price["periodEnd"] == ["202609220000"]
        assert price["securityToken"] == [TOKEN]

        load = _query_of(load_call)
        assert load["documentType"] == ["A65"]
        assert load["processType"] == ["A16"]
        assert load["outBiddingZone_Domain"] == ["10YGR-HTSO-----Y"]
        assert load["periodStart"] == ["202609200000"]

    @responses.activate
    def test_backfill_requests_whole_calendar_month_chunks(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        _register_documents()
        collector = entsoe_collector(uploader, tmp_staging)
        collector.fetch(
            get_region("greece_fire"), start_date=date(2026, 1, 15), end_date=date(2026, 4, 2)
        )

        # 4 chunks x 2 documents x 1 zone.
        assert len(responses.calls) == 8
        starts = {_query_of(call)["periodStart"][0] for call in responses.calls}
        assert starts == {"202601150000", "202602010000", "202603010000", "202604010000"}

    @responses.activate
    def test_requests_are_paced_under_the_hourly_quota(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        _register_documents()
        collector = entsoe_collector(uploader, tmp_staging)
        collector.fetch(
            get_region("greece_fire"), start_date=date(2026, 1, 15), end_date=date(2026, 4, 2)
        )
        # 8 requests: the very first goes out immediately, the rest wait.
        delays = collector._sleep.delays
        assert len(delays) == 7
        assert delays == [REQUEST_INTERVAL_S] * 7

    @responses.activate
    def test_adjacent_chunks_that_overlap_do_not_duplicate_rows(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        # Market days run 22:00Z-22:00Z, so ENTSO-E answers both chunks below
        # with the same points; without de-duplication the union would be 16.
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        _register_documents()
        collector = entsoe_collector(uploader, tmp_staging)
        frame = collector.fetch(
            get_region("greece_fire"), start_date=date(2026, 9, 30), end_date=date(2026, 10, 1)
        )
        assert len(responses.calls) == 4  # 2 chunks x 2 documents
        assert len(frame) == 8

    @responses.activate
    def test_empty_window_returns_no_rows_without_failing(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        responses.add(responses.GET, PRICE_URL, body=ACK_DOC, status=200)
        responses.add(responses.GET, LOAD_URL, body=ACK_DOC, status=200)
        collector = entsoe_collector(uploader, tmp_staging)
        summary = collector.run(
            [get_region("greece_fire")], start_date=date(2030, 1, 1), end_date=date(2030, 1, 1)
        )
        assert summary.results[0].status == "empty"
        assert summary.exit_code() == 0

    @responses.activate
    def test_run_end_to_end_counts_every_point(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        _register_documents()
        collector = entsoe_collector(uploader, tmp_staging)
        summary = collector.run(
            [get_region("greece_fire")], start_date=date(2026, 9, 20), end_date=date(2026, 9, 21)
        )
        assert summary.results[0].status == "success"
        assert summary.total_rows == 8
        assert summary.exit_code() == 0

    @responses.activate
    def test_missing_key_fails_the_region_loudly(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.delenv("ENTSOE_API_KEY", raising=False)
        monkeypatch.delenv("DATABRICKS_SECRET_SCOPE", raising=False)
        collector = entsoe_collector(uploader, tmp_staging)
        summary = collector.run(
            [get_region("greece_fire")], start_date=date(2030, 1, 1), end_date=date(2030, 1, 1)
        )
        result = summary.results[0]
        assert result.status == "failed"
        assert "ENTSOE_API_KEY" in (result.error or "")
        assert summary.exit_code() != 0

    @responses.activate
    def test_rate_limit_prose_is_transient_even_as_a_4xx(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        responses.add(
            responses.GET, PRICE_URL, body="Request rate exceeded, please retry later", status=400
        )
        collector = entsoe_collector(uploader, tmp_staging)
        with pytest.raises(TransientError) as excinfo:
            collector.fetch(
                get_region("greece_fire"), start_date=date(2026, 9, 20), end_date=date(2026, 9, 21)
            )
        # The URL carries securityToken as a query parameter; it must never
        # reach an error message.
        assert TOKEN not in str(excinfo.value)

    @responses.activate
    def test_invalid_token_is_permanent_not_a_retry_storm(
        self, uploader, tmp_staging, monkeypatch
    ) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", TOKEN)
        responses.add(responses.GET, PRICE_URL, body="Invalid access token provided", status=400)
        collector = entsoe_collector(uploader, tmp_staging)
        with pytest.raises(PermanentError) as excinfo:
            collector.fetch(
                get_region("greece_fire"), start_date=date(2026, 9, 20), end_date=date(2026, 9, 21)
            )
        assert TOKEN not in str(excinfo.value)


class TestBronzeContract:
    @staticmethod
    def _frame():
        points, unit = _parse_document(LOAD_DOC, document=DOCUMENTS[1])
        return pd.DataFrame(
            [
                {
                    "timestamp": stamp,
                    "latitude": 39.0,
                    "longitude": 22.0,
                    "metric_type": "actual_load",
                    "product": "A65_actual_load",
                    "value": value,
                    "unit": unit,
                    "zone_code": "10YGR-HTSO-----Y",
                    "spatial_scope": "bidding_zone",
                    "region_id": "greece_fire",
                }
                for stamp, value in points
            ]
        )

    def test_valid_rows_validate(self) -> None:
        validate_frame(self._frame(), BRONZE_ENTSOE)

    def test_metric_type_outside_the_contract_is_rejected(self) -> None:
        frame = self._frame()
        frame["metric_type"] = "fire_radiative_power"
        with pytest.raises(SchemaValidationError):
            validate_frame(frame, BRONZE_ENTSOE)

    def test_rows_without_a_zone_code_are_rejected(self) -> None:
        # zone_code is what keeps two zones mapped to one region apart; a null
        # must fail validation rather than quietly merge ES into PT.
        frame = conform_to_schema(self._frame().drop(columns=["zone_code"]), BRONZE_ENTSOE)
        with pytest.raises(SchemaValidationError, match="zone_code"):
            validate_frame(frame, BRONZE_ENTSOE)

    def test_spatial_scope_must_be_explicit(self) -> None:
        frame = self._frame()
        frame["spatial_scope"] = "point_measurement"
        with pytest.raises(SchemaValidationError):
            validate_frame(frame, BRONZE_ENTSOE)


class TestEstimate:
    def test_estimate_counts_chunks_zones_and_documents(self) -> None:
        regions = [get_region("greece_fire"), get_region("iberia_fire")]
        estimate = estimate_requests("entsoe", regions, date(2026, 1, 1), date(2026, 3, 31))
        assert estimate["windows"] == 3  # three calendar months
        assert estimate["zones"] == 3  # GR + ES + PT
        assert estimate["region_count"] == 2
        assert estimate["estimated_requests"] == 3 * 3 * len(DOCUMENTS)

    def test_estimate_honestly_reports_zero_for_unmapped_regions(self) -> None:
        estimate = estimate_requests("entsoe", [get_region("arctic")], date(2026, 1, 1), date(2026, 1, 31))
        assert estimate["estimated_requests"] == 0
        assert estimate["zones"] == 0


class TestRedaction:
    def test_security_token_query_parameter_is_redacted(self) -> None:
        url = (
            f"{ENTSOE_ENDPOINT}?securityToken={TOKEN}&documentType=A65"
            "&periodStart=202609200000"
        )
        redacted = redact_url(url)
        assert TOKEN not in redacted
        assert REDACTED in redacted
        assert "documentType=A65" in redacted
        assert TOKEN not in redact_text(f"failed to call {url}")

    def test_the_key_is_a_recognised_env_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("ENTSOE_API_KEY", "super-secret-entsoe-value")
        assert "super-secret-entsoe-value" in secret_values_from_env()

    def test_a_registered_key_value_never_survives_redaction(self) -> None:
        register_secret("another-entsoe-key-value")
        assert "another-entsoe-key-value" not in redact_text("x another-entsoe-key-value y")
