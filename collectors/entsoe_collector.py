"""ENTSO-E Transparency Platform: day-ahead electricity price + actual load.

Two documents per bidding zone, both quarter-hourly series:

1. **A44 / A01 day-ahead price** (`Publication_MarketDocument`, value element
   ``price.amount``, unit composed from ``currency_Unit.name`` + ``
   price_Measure_Unit.name`` -> EUR/MWH). Published the day before delivery, so
   a morning run sees yesterday's realised load *and* today's published price.
2. **A65 / A16 actual total load** (`GL_MarketDocument`, value element
   ``quantity``, unit label ``quantity_Measure_Unit.name`` -> MAW). Verified
   live: Greece reports 4386 for a 4.4 GW night, i.e. the "MAW" label carries
   megawatt-scale numbers.

Both are whole-zone aggregates, so rows are stamped with the study region's
bbox centre and ``spatial_scope="bidding_zone"`` — an attribution anchor, not a
measurement point (same convention as the hemispheric sea-ice rows). The
``zone_code`` rides on every row because ES and PT both map to iberia_fire and
must never merge into one series.

Operational notes, all learned against the live API:

- The platform quotas roughly 400 requests/hour, so every request after the
  first in a run waits ``REQUEST_INTERVAL_S`` (~9.5s -> <379/h sustained).
- Empty windows come back as an `Acknowledgement_MarketDocument` saying
  "No matching data found" — a legitimate outcome, mapped to zero rows rather
  than a failure (a future delivery day simply has no realised load yet).
- Market days run 22:00Z-22:00Z, so adjacent calendar-month chunks overlap by
  two hours and the fetched frame is de-duplicated before it is written.
- Positions restart at 1 inside each ``Period``; a timestamp is
  ``Period.timeInterval/start + (position - 1) * resolution``.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

from collectors.base_collector import BaseCollector, main_for
from collectors.config import Region, get_secret
from ops.redact import redact_text
from ops.resilience import PermanentError, TransientError
from pandera_schemas import BRONZE_ENTSOE

LOGGER = logging.getLogger(__name__)

#: Canonical endpoint. The older `web-api.transparency.entsoe.eu` host does not
#: resolve; `web-api.tp.entsoe.eu` is the current alias for the same API.
ENTSOE_ENDPOINT = "https://web-api.tp.entsoe.eu/api"

#: Sustained spacing between API calls. The platform's quota is ~400
#: requests/hour; 9.5s keeps a backfill under ~379/h even at back-to-back pace.
REQUEST_INTERVAL_S = 9.5

#: Body substrings that mark a throttled answer. The platform is documented as
#: quota-limited, and has been observed answering 4xx with a prose rate-limit
#: message rather than a 429 — that must retry, not fail the day permanently.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "rate exceeded",
    "too many request",
    "quota",
)

PRODUCT_PRICE = "A44_day_ahead_price"
PRODUCT_LOAD = "A65_actual_load"

#: Bidding zone (ENTSO-E area code) per study region. Codes verified live.
#: FR/DE/IT have codes on the platform too, but no study region's bbox contains
#: those zones, so they stay unmapped rather than be pinned to an arbitrary
#: region — add them when a region exists, not before.
ZONE_BY_REGION: dict[str, tuple[str, ...]] = {
    "greece_fire": ("10YGR-HTSO-----Y",),
    "iberia_fire": ("10YES-REE------0", "10YPT-REN------W"),
    "carpathian_deforest": ("10YRO-TEL------P",),
    "alps_ice": ("10YAT-APG------L",),
    "norway_ice": ("10YNO-0--------C",),
}


def _bbox_center(region: Region) -> tuple[float, float]:
    west, south, east, north = region.bbox
    return (south + north) / 2.0, (west + east) / 2.0


def iter_month_chunks(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Half-open ``[cursor, boundary)`` windows cut on calendar-month edges.

    ``end`` is inclusive (the CLI's convention); ``periodEnd`` is exclusive, so
    the effective limit is the following midnight. Month-sized requests keep a
    multi-year backfill from asking for one enormous range, and match how the
    platform's own documents are laid out.
    """
    cursor = start
    limit = end + timedelta(days=1)
    while cursor < limit:
        if cursor.month == 12:
            boundary = date(cursor.year + 1, 1, 1)
        else:
            boundary = date(cursor.year, cursor.month + 1, 1)
        boundary = min(boundary, limit)
        yield cursor, boundary
        cursor = boundary


def _local(tag: str) -> str:
    """Element local name — the platform namespaces each document family."""
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element | None, local_name: str) -> ET.Element | None:
    if element is None:
        return None
    for candidate in element:
        if _local(candidate.tag) == local_name:
            return candidate
    return None


def _child_text(element: ET.Element | None, local_name: str) -> str | None:
    found = _child(element, local_name)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _first_text(root: ET.Element, local_name: str) -> str | None:
    for element in root.iter():
        if _local(element.tag) == local_name and element.text and element.text.strip():
            return element.text.strip()
    return None


def _resolution_to_timedelta(resolution: str) -> pd.Timedelta:
    """ISO-8601 duration subset used by the platform: P1D, PT60M, PT30M, PT15M."""
    text = (resolution or "").strip().upper()
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", text)
    if not match or not any(match.groups()):
        raise PermanentError(f"entsoe: unsupported resolution {resolution!r}")
    days, hours, minutes = (int(group or 0) for group in match.groups())
    # Build from total minutes with an explicit unit: the kwargs constructor
    # (Timedelta(days=1, ...)) goes through a bare-integer path that pandas
    # deprecates as a "generic" timedelta unit.
    delta = pd.to_timedelta(days * 1440 + hours * 60 + minutes, unit="m")
    if delta <= pd.to_timedelta(0, unit="m"):
        raise PermanentError(f"entsoe: non-positive resolution {resolution!r}")
    return delta


def _period_start(period: ET.Element) -> pd.Timestamp:
    start_text = _child_text(_child(period, "timeInterval"), "start")
    if not start_text:
        raise PermanentError("entsoe: Period without timeInterval/start")
    stamp = pd.Timestamp(start_text)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _price_unit(root: ET.Element) -> str | None:
    """EUR/MWH composed from ``currency_Unit.name`` + ``price_Measure_Unit.name``."""
    currency = _first_text(root, "currency_Unit.name")
    measure = _first_text(root, "price_Measure_Unit.name")
    if currency and measure:
        return f"{currency}/{measure}"
    return currency or measure or None


def _load_unit(root: ET.Element) -> str | None:
    """Verbatim ``quantity_Measure_Unit.name`` (MAW); staging normalises to MW."""
    return _first_text(root, "quantity_Measure_Unit.name")


@dataclass(frozen=True)
class _Document:
    """One ENTSO-E document family: request parameters + parse rules."""

    product: str
    metric_type: str
    document_type: str
    process_type: str
    roots: tuple[str, ...]
    value_tag: str
    unit_fn: Callable[[ET.Element], str | None]
    #: "in_out" fills In_Domain+Out_Domain (A44); "bidding_zone" fills
    #: outBiddingZone_Domain (A65).
    domain_kind: str

    def domain_params(self, zone: str) -> dict[str, str]:
        if self.domain_kind == "in_out":
            return {"In_Domain": zone, "Out_Domain": zone}
        return {"outBiddingZone_Domain": zone}

    def params(self, zone: str, token: str, start: date, end: date) -> dict[str, Any]:
        return {
            "securityToken": token,
            "documentType": self.document_type,
            "processType": self.process_type,
            "periodStart": start.strftime("%Y%m%d%H%M"),
            "periodEnd": end.strftime("%Y%m%d%H%M"),
            **self.domain_params(zone),
        }


DOCUMENTS: tuple[_Document, ...] = (
    _Document(
        product=PRODUCT_PRICE,
        metric_type="day_ahead_price",
        document_type="A44",
        process_type="A01",
        roots=("Publication_MarketDocument",),
        value_tag="price.amount",
        unit_fn=_price_unit,
        domain_kind="in_out",
    ),
    _Document(
        product=PRODUCT_LOAD,
        metric_type="actual_load",
        document_type="A65",
        process_type="A16",
        roots=("GL_MarketDocument",),
        value_tag="quantity",
        unit_fn=_load_unit,
        domain_kind="bidding_zone",
    ),
)


def _parse_document(
    text: str, *, document: _Document
) -> tuple[list[tuple[pd.Timestamp, float]], str | None]:
    """Parse one market document into ``(points, verbatim unit label)``.

    An empty window is answered with an `Acknowledgement_MarketDocument`
    ("No matching data found …") and yields no points — that is a legitimate
    outcome, not an error. Everything else that does not look like the expected
    document fails loudly: silently dropping a renamed element would hollow out
    the series without anyone noticing.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        if "No matching data" in (text or ""):
            return [], None
        raise PermanentError(f"entsoe: unparseable response: {redact_text(str(exc))}") from exc

    if _local(root.tag) not in document.roots:
        if "No matching data" in (text or ""):
            return [], None
        reasons = [
            element.text.strip()
            for element in root.iter()
            if _local(element.tag).startswith("reason")
            and element.text
            and element.text.strip()
        ]
        detail = "; ".join(reasons) or f"unexpected document <{_local(root.tag)}>"
        raise PermanentError(f"entsoe {document.document_type}: {redact_text(detail)}")

    unit = document.unit_fn(root)
    points: list[tuple[pd.Timestamp, float]] = []
    points_seen = 0

    for period in (element for element in root.iter() if _local(element.tag) == "Period"):
        resolution = _child_text(period, "resolution")
        if not resolution:
            raise PermanentError("entsoe: Period without resolution")
        delta = _resolution_to_timedelta(resolution)
        start = _period_start(period)

        for point in (element for element in period if _local(element.tag) == "Point"):
            points_seen += 1
            position_text = _child_text(point, "position")
            value_element = _child(point, document.value_tag)
            if value_element is None or not (value_element.text or "").strip():
                # The slot exists but carries no observation; skip, never invent.
                continue
            try:
                position = int(position_text)
            except (TypeError, ValueError) as exc:
                raise PermanentError(
                    f"entsoe: Point with unusable position {position_text!r}"
                ) from exc
            if position < 1:
                raise PermanentError(f"entsoe: Point position {position} is not 1-based")
            try:
                value = float(value_element.text.strip())
            except ValueError as exc:
                raise PermanentError(
                    f"entsoe: non-numeric value {value_element.text!r}"
                ) from exc
            points.append((start + (position - 1) * delta, value))

    if points_seen and not points:
        # Points parsed but every value was missing: the element was renamed
        # (the platform has shipped `Price.amount` vs `price.amount` variants).
        raise PermanentError(
            f"entsoe: {points_seen} Point(s) but no <{document.value_tag}> value — "
            "element name drift?"
        )
    return points, unit


class EntsoeCollector(BaseCollector):
    source_id = "entsoe"
    #: Multi-metric source (price + load): every row carries its own metric_type,
    #: so — like sentinel — the class default stays empty rather than lying for
    #: half the rows.
    metric_type = ""
    label = "ENTSO-E day-ahead price + actual load"
    raw_schema = BRONZE_ENTSOE
    time_column = "timestamp"
    default_region_ids = tuple(ZONE_BY_REGION)
    #: Live window: yesterday's realised load plus today's published day-ahead
    #: price; two days back covers the 22:00Z market-day boundary.
    live_lookback_days = 2

    def __init__(self, *, request_interval_s: float = REQUEST_INTERVAL_S, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.request_interval_s = request_interval_s

    def _raise_for_status(self, response: requests.Response, safe_url: str) -> None:
        """Treat rate/quota prose as transient even when it arrives as a 4xx.

        The base class already retries 429 and 5xx; this extends the same
        treatment to throttling the platform reports in the body, so a burst of
        requests costs a backoff instead of a permanently failed region.
        """
        if response.status_code >= 400:
            lowered = (response.text or "").lower()
            if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
                body = redact_text((response.text or "")[:300]).replace("\n", " ")
                raise TransientError(f"HTTP {response.status_code} from {safe_url}: {body}")
        super()._raise_for_status(response, safe_url)

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
            raise PermanentError(f"entsoe: start {start} is after end {end}")

        zones = ZONE_BY_REGION.get(region.region_id, ())
        if not zones:
            self.log(
                logging.WARNING,
                f"entsoe/{region.region_id}: no bidding zone mapped, skipping",
                source_id=self.source_id,
                region_id=region.region_id,
            )
            return pd.DataFrame()

        token = get_secret("ENTSOE_API_KEY")
        latitude, longitude = _bbox_center(region)
        rows: list[dict[str, Any]] = []

        for zone in zones:
            for document in DOCUMENTS:
                for chunk_start, chunk_end in iter_month_chunks(start, end):
                    if self.requests_made:
                        # Stay under the platform's ~400 requests/hour quota.
                        self._sleep(self.request_interval_s)
                    text = self.get_text(
                        ENTSOE_ENDPOINT,
                        params=document.params(zone, token, chunk_start, chunk_end),
                    )
                    points, unit = _parse_document(text, document=document)
                    for stamp, value in points:
                        rows.append(
                            {
                                "timestamp": stamp,
                                "latitude": latitude,
                                "longitude": longitude,
                                "metric_type": document.metric_type,
                                "product": document.product,
                                "value": value,
                                "unit": unit,
                                "zone_code": zone,
                                "spatial_scope": "bidding_zone",
                                "region_id": region.region_id,
                            }
                        )

        if not rows:
            return pd.DataFrame()
        # Market days run 22:00Z-22:00Z, so adjacent month chunks overlap by two
        # hours and would otherwise write the same interval twice.
        return pd.DataFrame(rows).drop_duplicates()


if __name__ == "__main__":
    raise SystemExit(main_for(EntsoeCollector))
