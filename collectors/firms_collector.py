"""NASA FIRMS active fire detection collector.

The area API caps a request at 5 days, so a fetch walks its date range in 5-day
slices. Live runs use near-real-time products (``*_NRT``); backfills use
standard-processing products (``*_SP``) because the NRT archive is pruned.

Confidence arrives in two incompatible encodings — VIIRS ships ``l``/``n``/``h``
while MODIS ships 0-100 — so both are normalised to a level plus a percentage at
ingestion. Downstream models then never have to know which instrument a row came
from, which matters because the same fire is routinely detected by several.

Fire detection is inherently duplicated across products and overpasses: this
collector lands everything (bronze is append-only by design) and the staging
models deduplicate on ``(product, acq_datetime, latitude, longitude)``.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

import pandas as pd

from collectors.base_collector import BaseCollector, main_for
from collectors.config import DATA_DIR, Region, get_secret
from collectors.time_utils import iter_date_windows
from ops.redact import redact_text
from ops.resilience import PermanentError, TransactionBudget
from pandera_schemas import BRONZE_FIRMS

LOGGER = logging.getLogger(__name__)

#: NASA FIRMS runs two archives per instrument, and neither covers everything.
#:
#: Measured against the live API on 2026-09-21 (Iberia, 5-day windows):
#:
#:   window start   VIIRS_SNPP_SP   MODIS_SP   VIIRS_SNPP_NRT
#:   2026-06-01           249          74            0     <- NRT already expired
#:   2026-06-15           236          41           --
#:   2026-07-01             0           0         2267     <- SP not yet processed
#:   2026-08-01             0           0          368
#:   2026-09-10             0           0          413
#:
#: So SP is published roughly three months behind real time and NRT is *retained*
#: for roughly three months. Selecting products by "is this a backfill?" therefore
#: returns silently empty data for the most recent quarter — the period that
#: matters most. Selection is instead driven by window age, with a fallback through
#: the overlap so a moving boundary cannot open a gap.
SP_MIN_AGE_DAYS = 100
NRT_MAX_AGE_DAYS = 112

#: Local mirror of FIRMS' server-side quota window, shared across processes.
QUOTA_STATE_PATH = DATA_DIR / "state" / "firms_quota.json"

#: VIIRS categorical confidence plus the nominal percentage it stands for.
_CONFIDENCE_BY_CODE: dict[str, tuple[str, float]] = {
    "l": ("low", 30.0),
    "low": ("low", 30.0),
    "n": ("nominal", 60.0),
    "nominal": ("nominal", 60.0),
    "h": ("high", 90.0),
    "high": ("high", 90.0),
}

_LOW_BELOW = 30.0
_HIGH_AT_OR_ABOVE = 80.0

_NUMERIC_COLUMNS = (
    "latitude",
    "longitude",
    "brightness",
    "bright_ti4",
    "bright_ti5",
    "frp",
    "scan",
    "track",
    "type",
)

#: Minimum columns every FIRMS product must provide.
_REQUIRED_COLUMNS = ("latitude", "longitude", "acq_date", "acq_time", "confidence")


def normalise_confidence(value: Any) -> tuple[str, float]:
    """Map a FIRMS confidence value to ``(level, percent)``.

    Unparseable values raise rather than defaulting to a mid-range guess: a
    silent default here would quietly inflate the confidence distribution the
    anomaly model learns from.
    """
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        raise PermanentError("FIRMS row is missing its confidence value")
    text = str(value).strip().lower()
    if text in _CONFIDENCE_BY_CODE:
        return _CONFIDENCE_BY_CODE[text]
    try:
        pct = float(text)
    except (TypeError, ValueError) as exc:
        raise PermanentError(f"unrecognised FIRMS confidence value {value!r}") from exc

    pct = min(max(pct, 0.0), 100.0)
    if pct < _LOW_BELOW:
        return "low", pct
    if pct >= _HIGH_AT_OR_ABOVE:
        return "high", pct
    return "nominal", pct


def product_plan(
    pairs: Sequence[tuple[str, str]],
    window_end: date,
    today: date,
    *,
    sp_min_age_days: int = SP_MIN_AGE_DAYS,
    nrt_max_age_days: int = NRT_MAX_AGE_DAYS,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Choose which products cover a window: ``(preferred, alternative)``.

    Standard processing is preferred where it exists because it is the reprocessed
    product. Where SP is not yet available, or NRT is about to expire, the other
    archive is offered as a fallback so the moving boundary cannot leave a hole.
    Both empty means nothing can cover the window — which the caller reports rather
    than passing off as "no fires".
    """
    age_days = (today - window_end).days

    sp_available = age_days >= sp_min_age_days
    nrt_available = age_days <= nrt_max_age_days
    if not sp_available and not nrt_available:
        return (), ()

    if sp_available and nrt_available:
        preferred, alternative = "sp", "nrt"
    elif sp_available:
        # Too old for NRT; SP is the only archive left.
        preferred, alternative = "sp", None
    else:
        preferred, alternative = "nrt", None

    def pick(kind: str) -> tuple[str, ...]:
        return tuple(pair[0] if kind == "sp" else pair[1] for pair in pairs)

    return pick(preferred), (pick(alternative) if alternative else ())


class FirmsCollector(BaseCollector):
    """NASA FIRMS active fire detections (area API, 5-day windows)."""

    source_id = "firms"
    metric_type = "fire_radiative_power"
    label = "NASA FIRMS active fire"
    raw_schema = BRONZE_FIRMS
    time_column = "acq_datetime"
    default_region_ids = ("iberia_fire", "greece_fire")

    api_base = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
    max_day_range = 5
    #: FIRMS documents 5000 transactions per 10 minutes and warns that one request
    #: may cost several; measurement says a 5-day window costs about 5. Staying
    #: under the quota is handled by the budget below, so this is only a small
    #: inter-request courtesy pause.
    request_pause_s = 0.05
    #: FIRMS documents 5000 transactions per 10 minutes, but *measured* behaviour is
    #: far tighter than that: bursts of ~90-180 requests (450-900 transactions) were
    #: throttled with HTTP 400, and each burst appears to earn a slower-cooling
    #: penalty. The documented figure is therefore treated as an upper bound to stay
    #: well under, not as a target to hit. 1500 transactions per 10 minutes allows
    #: ~300 requests (one every two seconds); a two-year two-region backfill takes
    #: around half an hour at that pace, which is a fine price for not being blocked.
    #: Override with FIRMS_QUOTA_LIMIT when you know your key's real allowance.
    quota_limit = int(os.environ.get("FIRMS_QUOTA_LIMIT", "1500"))
    quota_window_s = 600.0
    #: Failures are counted per attempt (4 attempts per request), so 12 tolerates
    #: three fully-failed requests before the run gives up on the source.
    breaker_failure_threshold = 12
    #: Observed in a 900-request backfill: a single request came back
    #: ``HTTP 400 Invalid MAP_KEY`` while the same key succeeded either side of it,
    #: and a re-issue of that request worked. Treating it as permanent would drop a
    #: whole region's data; the retry budget and breaker bound the cost if the key
    #: really is bad.
    transient_client_errors = (400,)

    def __init__(
        self,
        *,
        map_key: str | None = None,
        max_requests: int = 4000,
        live_lookback_days: int = 2,
        request_pause_s: float | None = None,
        budget: TransactionBudget | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.map_key = map_key or get_secret("FIRMS_MAP_KEY")
        self.max_requests = max_requests
        self.live_lookback_days = live_lookback_days
        if request_pause_s is not None:
            self.request_pause_s = request_pause_s
        # A 5-day window costs ~5 transactions against the per-10-minute quota.
        self.budget = budget or TransactionBudget(
            limit=self.quota_limit,
            window_seconds=self.quota_window_s,
            sleep=self._sleep,
            # Persisted so a re-run shares the server's sliding window instead of
            # starting with an empty local budget and throttling immediately.
            state_path=QUOTA_STATE_PATH,
        )

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
        pairs = self.spec.product_pairs
        if not pairs:
            raise PermanentError(f"{self.source_id} has no product pairs configured")

        end = end_date or self.now().date()
        start = start_date or (end - timedelta(days=self.live_lookback_days))
        if start > end:
            raise PermanentError(f"start_date {start} is after end_date {end}")

        windows = iter_date_windows(start, end, size_days=self.max_day_range)
        self.log(
            logging.INFO,
            f"firms/{region.region_id}: {len(windows)} window(s) from {start} to {end} "
            f"({'backfill' if backfill else 'live'})",
            source_id=self.source_id,
            region_id=region.region_id,
        )

        frames: list[pd.DataFrame] = []
        uncovered: list[str] = []
        fallbacks = 0
        truncated = False

        for window_start, window_end in windows:
            preferred, alternative = product_plan(pairs, window_end, self.now().date())
            if not preferred:
                # Too new for SP and too old for NRT. Reported, never silently
                # treated as a fire-free window.
                uncovered.append(f"{window_start}..{window_end}")
                continue

            collected: list[pd.DataFrame] = []
            for index, group in enumerate((preferred, alternative)):
                if not group:
                    continue
                collected = []
                for product in group:
                    if self.requests_made >= self.max_requests:
                        truncated = True
                        break
                    days = (window_end - window_start).days + 1
                    self.budget.spend(days)
                    text = self._download(product, region, window_start, days)
                    chunk = self._parse(text, product=product)
                    if not chunk.empty:
                        collected.append(chunk)
                    if self.request_pause_s:
                        self._sleep(self.request_pause_s)
                if collected:
                    if index == 1:
                        fallbacks += 1
                        self.log(
                            logging.INFO,
                            f"firms/{region.region_id}: window {window_start}..{window_end} "
                            f"was empty in the preferred archive; used {group[0]} instead",
                            source_id=self.source_id,
                            region_id=region.region_id,
                        )
                    frames.extend(collected)
                    break
            if truncated:
                break

        if truncated:
            LOGGER.warning(
                "firms/%s: stopped at max_requests=%d — raise the cap or narrow the window",
                region.region_id,
                self.max_requests,
            )
        if uncovered:
            LOGGER.warning(
                "firms/%s: %d window(s) covered by neither archive (%s) — SP lags ~%dd "
                "and NRT is retained ~%dd; re-run once processing catches up",
                region.region_id,
                len(uncovered),
                uncovered[0],
                SP_MIN_AGE_DAYS,
                NRT_MAX_AGE_DAYS,
            )
        if fallbacks:
            self.log(
                logging.INFO,
                f"firms/{region.region_id}: {fallbacks} window(s) covered by the fallback archive",
                source_id=self.source_id,
                region_id=region.region_id,
            )

        if not frames:
            return pd.DataFrame(columns=list(_REQUIRED_COLUMNS))
        return pd.concat(frames, ignore_index=True)

    def _download(self, product: str, region: Region, start: date, days: int) -> str:
        url = f"{self.api_base}/{self.map_key}/{product}/{region.bbox_csv()}/{days}/{start.isoformat()}"
        return self.get_text(url)

    def _parse(self, text: str, *, product: str) -> pd.DataFrame:
        stripped = (text or "").strip()
        if not stripped:
            return pd.DataFrame()

        header = stripped.splitlines()[0].lower()
        if "latitude" not in header:
            raise PermanentError(
                f"FIRMS returned a non-CSV payload for {product}: {redact_text(stripped[:200])}"
            )

        frame = pd.read_csv(io.StringIO(stripped), dtype=str, skipinitialspace=True)
        if frame.empty:
            return frame
        frame.columns = [str(column).strip().lower() for column in frame.columns]
        frame["product"] = product
        return frame

    # -- normalise ---------------------------------------------------------

    def normalize(self, frame: pd.DataFrame, region: Region, **_kwargs: Any) -> pd.DataFrame:
        if frame.empty:
            return frame

        out = frame.copy()
        missing = [column for column in _REQUIRED_COLUMNS if column not in out.columns]
        if missing:
            raise PermanentError(
                f"FIRMS payload is missing required column(s) {missing}; "
                f"received {sorted(out.columns)}"
            )

        for column in _NUMERIC_COLUMNS:
            if column in out.columns:
                out[column] = pd.to_numeric(out[column], errors="coerce")

        out = out.dropna(subset=["latitude", "longitude"])

        out["acq_date"] = out["acq_date"].astype(str).str.strip()
        out["acq_time"] = (
            out["acq_time"]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.zfill(4)
        )
        out = out[out["acq_date"].str.match(r"^\d{4}-\d{2}-\d{2}$") & out["acq_time"].str.match(r"^\d{4}$")]
        if out.empty:
            raise PermanentError(
                f"FIRMS payload for {region.region_id} has no rows with a parseable acquisition date/time"
            )

        # A single malformed upstream timestamp should not condemn an entire
        # multi-year backfill, but dropping rows silently would be worse — so
        # individual failures are counted and reported, and an all-failure run stops.
        out["acq_datetime"] = pd.to_datetime(
            out["acq_date"] + " " + out["acq_time"],
            format="%Y-%m-%d %H%M",
            utc=True,
            errors="coerce",
        )
        unparseable = out["acq_datetime"].isna()
        if unparseable.all():
            raise PermanentError(
                f"FIRMS acquisition timestamps are unparseable for {region.region_id}: "
                f"e.g. {out['acq_date'].iloc[0]!r} {out['acq_time'].iloc[0]!r}"
            )
        if unparseable.any():
            LOGGER.warning(
                "firms/%s: dropping %d row(s) with unparseable acquisition timestamps",
                region.region_id,
                int(unparseable.sum()),
            )
            out = out.loc[~unparseable]

        decoded = [normalise_confidence(value) for value in out["confidence"]]
        out["confidence"] = [level for level, _ in decoded]
        out["confidence_pct"] = [pct for _, pct in decoded]

        if "daynight" in out.columns:
            daynight = out["daynight"].astype(str).str.strip().str.upper().str[:1]
            out["daynight"] = daynight.where(daynight.isin(["D", "N"]), None)

        for column in ("satellite", "instrument", "version"):
            if column in out.columns:
                out[column] = out[column].astype(str).str.strip().replace({"nan": None, "": None})

        for column in ("scan", "track", "frp", "brightness", "bright_ti4", "bright_ti5"):
            if column in out.columns:
                negative = out[column] < 0
                if negative.any():
                    out.loc[negative, column] = None

        return out.reset_index(drop=True)


if __name__ == "__main__":
    raise SystemExit(main_for(FirmsCollector))
