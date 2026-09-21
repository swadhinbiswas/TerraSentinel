"""Shared collection machinery: config, HTTP, retry, circuit breaking, writing.

Every collector subclasses :class:`BaseCollector` and implements only what is
genuinely source-specific — ``fetch()`` (call the API) and optionally
``normalize()`` (reshape the payload). Everything else is inherited and identical
across sources:

    fetch -> normalize -> validate_raw -> enrich (H3 + provenance)
          -> validate_envelope -> write parquet -> upload to HF bronze

Two behaviours deserve to be called out because they are what separates this from
a script that calls an API once:

* **A circuit breaker per source.** When FIRMS is down it is down for everyone;
  without a breaker the workflow retries until the job times out and you learn
  nothing. With one, the run ends in seconds reporting ``circuit_open``.
* **Fail-loud validation at both ends.** The raw payload is checked against a
  source schema and the enriched frame against the provenance envelope, so a
  silent upstream column rename surfaces at ingestion instead of three layers
  downstream as a null-filled dashboard.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import pandas as pd
import pandera as pa
import requests

from collectors.config import (
    BRONZE_STAGING_DIR,
    REGIONS,
    SOURCES,
    Region,
    bronze_filename,
    bronze_prefix,
    get_region,
    get_secret,
    run_fingerprint,
)
from collectors.geo_utils import add_h3_index
from ops.redact import redact_text, redact_url
from ops.resilience import (
    CircuitBreaker,
    CircuitBreakerOpen,
    PermanentError,
    RetryPolicy,
    TransientError,
    retry_call,
)
from pandera_schemas import BRONZE_ENVELOPE, METRIC_UNITS, conform_to_schema, validate_frame

__all__ = [
    "BaseCollector",
    "CollectionResult",
    "Uploader",
    "WriteResult",
    "configure_logging",
    "select_regions",
]

LOGGER = logging.getLogger("terrasentinel.collectors")

DEFAULT_TIMEOUT = 60.0
_STRUCTURED_KEYS = (
    "source_id",
    "region_id",
    "status",
    "rows",
    "path_in_repo",
    "error_type",
    "duration_s",
)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Minimal structured formatter — one JSON object per line, ready for CI logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key in _STRUCTURED_KEYS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(
    level: str | int = "INFO",
    *,
    json_output: bool | None = None,
    stream: Any = None,
) -> None:
    """Configure process logging, defaulting to JSON inside GitHub Actions."""
    if json_output is None:
        json_output = os.environ.get("LOG_JSON") == "1" or os.environ.get("GITHUB_ACTIONS") == "true"

    handler = logging.StreamHandler(stream or sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else level.upper())


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass(slots=True)
class WriteResult:
    rows: int
    local_path: Path | None = None
    path_in_repo: str | None = None
    commit_sha: str | None = None
    parquet_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "local_path": str(self.local_path) if self.local_path else None,
            "path_in_repo": self.path_in_repo,
            "commit_sha": self.commit_sha,
            "parquet_bytes": self.parquet_bytes,
        }


@dataclass(slots=True)
class CollectionResult:
    """Outcome of one (collector, region) unit of work."""

    source_id: str
    region_id: str
    status: str  # success | empty | failed | circuit_open
    rows: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    path_in_repo: str | None = None
    local_path: str | None = None
    error_type: str | None = None
    error: str | None = None
    requests_made: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"success", "empty"}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CollectionSummary:
    source_id: str
    started_at: str
    finished_at: str
    duration_s: float
    results: list[CollectionResult] = field(default_factory=list)
    breaker: dict[str, Any] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(result.rows for result in self.results)

    @property
    def failed(self) -> list[CollectionResult]:
        return [result for result in self.results if not result.ok]

    def exit_code(self) -> int:
        if not self.results or len(self.failed) == len(self.results):
            return 2
        if self.failed:
            return 1
        return 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 3),
            "total_rows": self.total_rows,
            "breaker": self.breaker,
            "results": [result.as_dict() for result in self.results],
        }


@runtime_checkable
class Uploader(Protocol):
    """Anything that can land a local file in object storage (HF Hub in practice)."""

    def upload_file(
        self, local_path: str | Path, path_in_repo: str, *, commit_message: str = ""
    ) -> Any: ...


# --------------------------------------------------------------------------
# Base collector
# --------------------------------------------------------------------------

class BaseCollector(ABC):
    """Template for a source collector. Subclasses implement ``fetch``."""

    source_id: ClassVar[str]
    metric_type: ClassVar[str] = ""
    label: ClassVar[str] = ""
    raw_schema: ClassVar[pa.DataFrameSchema]
    default_timeout: ClassVar[float] = DEFAULT_TIMEOUT
    #: Column holding the observation timestamp. Drives partition splitting, so a
    #: fetch spanning a day/month boundary lands as correctly shaped files.
    time_column: ClassVar[str] = ""
    #: Regions used when ``--regions all``; empty means "every region this
    #: collector's anomaly types cover".
    default_region_ids: ClassVar[tuple[str, ...]] = ()
    #: Failed attempts tolerated before the breaker opens. Attempts, not logical
    #: requests — see CircuitBreaker.failure_threshold.
    breaker_failure_threshold: ClassVar[int] = 5
    #: Client-error status codes this source also uses for *transient* conditions.
    #:
    #: FIRMS returns ``HTTP 400 "Invalid MAP_KEY"`` intermittently mid-backfill
    #: while the same key succeeds on adjacent requests, so a bare 4xx-is-permanent
    #: rule silently drops data. Anything listed here is retried; a genuinely bad
    #: credential still fails after the retry budget and trips the breaker.
    transient_client_errors: ClassVar[tuple[int, ...]] = ()

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        uploader: Uploader | None = None,
        breaker: CircuitBreaker | None = None,
        policy: RetryPolicy | None = None,
        staging_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Any = None,
        now: Callable[[], datetime] | None = None,
        run_id: str | None = None,
        dry_run: bool = False,
    ) -> None:
        if self.source_id not in SOURCES:
            raise ValueError(f"{type(self).__name__}.source_id={self.source_id!r} is not in SOURCES")
        self.spec = SOURCES[self.source_id]
        self.session = session if session is not None else requests.Session()
        self.uploader = uploader
        self.breaker = (
            breaker
            if breaker is not None
            else CircuitBreaker(self.source_id, failure_threshold=self.breaker_failure_threshold)
        )
        self.policy = policy if policy is not None else RetryPolicy()
        self.staging_dir = staging_dir or BRONZE_STAGING_DIR
        self._deferred_uploads: list[WriteResult] = []
        self._sleep = sleep
        self._rng = rng
        self._now = now or (lambda: datetime.now(UTC))
        self.dry_run = dry_run
        self.run_id = run_id or run_fingerprint(
            [self.source_id, self._now().isoformat(timespec="seconds")], length=16
        )
        self.requests_made = 0

    # -- small utilities ---------------------------------------------------

    def now(self) -> datetime:
        return self._now()

    def log(self, level: int, message: str, **fields: Any) -> None:
        LOGGER.log(level, redact_text(message), extra=fields)

    @property
    def default_headers(self) -> dict[str, str]:
        from collectors.config import DEFAULT_USER_AGENT

        return {"User-Agent": DEFAULT_USER_AGENT, "Accept-Encoding": "gzip"}

    # -- HTTP --------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json_body: Any = None,
        timeout: float | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """HTTP request with retry/backoff, breaker accounting and URL redaction."""
        safe_url = redact_url(url)
        merged_headers = {**self.default_headers, **(headers or {})}

        def attempt() -> requests.Response:
            self.requests_made += 1
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=merged_headers,
                    data=data,
                    json=json_body,
                    timeout=timeout or self.default_timeout,
                    stream=stream,
                )
            except requests.Timeout as exc:
                raise TransientError(f"timeout after {timeout or self.default_timeout}s: {safe_url}") from exc
            except requests.ConnectionError as exc:
                raise TransientError(f"connection error: {safe_url}: {exc}") from exc
            except requests.RequestException as exc:
                raise TransientError(f"request failed: {safe_url}: {exc}") from exc
            self._raise_for_status(response, safe_url)
            return response

        return retry_call(
            attempt,
            policy=self.policy,
            breaker=self.breaker,
            sleep=self._sleep,
            rng=self._rng,
            description=f"{self.source_id} {method} {safe_url}",
        )

    def _raise_for_status(self, response: requests.Response, safe_url: str) -> None:
        status = response.status_code
        if status < 400:
            return

        body = redact_text((response.text or "")[:300]).replace("\n", " ")
        hint = ""
        if status in (401, 403):
            hint = (
                f" — check the credentials for {self.source_id}: "
                f"{', '.join(self.spec.required_secrets) or 'none required'}"
            )

        if status == 429 or status >= 500 or status in self.transient_client_errors:
            raise TransientError(f"HTTP {status} from {safe_url}: {body}{hint}")
        raise PermanentError(f"HTTP {status} from {safe_url}: {body}{hint}")

    def get_text(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> str:
        return self.request("GET", url, params=params, timeout=timeout).text

    # -- pipeline stages ---------------------------------------------------

    @abstractmethod
    def fetch(self, region: Region, **kwargs: Any) -> pd.DataFrame:
        """Call the upstream API for one region and return a raw frame."""

    def normalize(self, frame: pd.DataFrame, region: Region, **kwargs: Any) -> pd.DataFrame:
        """Reshape a raw payload into the source's bronze column contract."""
        return frame

    def validate_raw(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Conform to the declared column set, then validate values.

        Conforming first means a payload missing an optional column (MODIS has
        ``brightness``, VIIRS does not) produces a uniformly shaped landing rather
        than one whose schema depends on which product happened to land.
        """
        conformed = conform_to_schema(frame, self.raw_schema)
        return validate_frame(conformed, self.raw_schema, label=f"bronze/{self.source_id}")

    def enrich(
        self,
        frame: pd.DataFrame,
        region: Region,
        *,
        observed_at: datetime | None = None,
        extra_columns: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Attach H3 index, region, metric, unit and provenance to every row."""
        enriched = add_h3_index(frame, region.h3_resolution)

        if "region_id" not in enriched.columns or enriched["region_id"].isna().all():
            enriched["region_id"] = region.region_id
        else:
            enriched["region_id"] = enriched["region_id"].fillna(region.region_id)

        if self.metric_type:
            enriched["metric_type"] = enriched.get("metric_type", self.metric_type)
        if "unit" not in enriched.columns:
            enriched["unit"] = enriched["metric_type"].map(METRIC_UNITS)
        else:
            enriched["unit"] = enriched["unit"].fillna(enriched["metric_type"].map(METRIC_UNITS))

        enriched["source_id"] = self.source_id
        stamp = (observed_at or self.now()).astimezone(UTC)
        if stamp.tzinfo is None:  # pragma: no cover - defensive
            raise PermanentError(
                f"{self.source_id}: ingestion timestamps must be timezone-aware; got a naive "
                "datetime. Assuming local time here would shift every observation."
            )
        enriched["ingested_at"] = stamp
        enriched["run_id"] = self.run_id

        for column, value in (extra_columns or {}).items():
            enriched[column] = value
        return enriched

    def validate_envelope(self, frame: pd.DataFrame) -> pd.DataFrame:
        return validate_frame(frame, BRONZE_ENVELOPE, label=f"bronze/{self.source_id}/envelope")

    def write_partitions(
        self,
        frame: pd.DataFrame,
        region: Region,
        *,
        backfill: bool = False,
        commit_message: str | None = None,
    ) -> list[WriteResult]:
        """Split a landing into period partitions and write each one.

        Backfill runs partition monthly, live runs daily. The period comes from
        the observation timestamp rather than wall-clock time, so re-running a
        historical window overwrites the same paths instead of duplicating rows.
        """
        if frame.empty:
            return []
        if not self.time_column or self.time_column not in frame.columns:
            raise PermanentError(
                f"{type(self).__name__}.time_column={self.time_column!r} is missing from the "
                "frame; collectors must declare the observation timestamp column "
                "used for partitioning"
            )

        stamps = pd.to_datetime(frame[self.time_column], utc=True, errors="raise")
        # Periods are timezone-naive by construction; convert to UTC first so a
        # row is never bucketed into the previous day by a local-time offset.
        period = stamps.dt.tz_convert("UTC").dt.tz_localize(None).dt.to_period(
            "M" if backfill else "D"
        )

        # A backfill produces dozens of monthly files; pushing each as its own Hub
        # commit wastes the repo's commit budget, so they are staged locally and
        # landed in a single folder commit once the run finishes.
        defer = (
            backfill
            and self.uploader is not None
            and not self.dry_run
            and hasattr(self.uploader, "upload_folder")
        )

        results: list[WriteResult] = []
        for value in sorted(period.unique()):
            chunk = frame.loc[period == value].reset_index(drop=True)
            result = self.write_bronze(
                chunk,
                region,
                partition_date=value.to_timestamp().date(),
                backfill=backfill,
                commit_message=commit_message,
                upload=not defer,
            )
            results.append(result)
            if defer:
                self._deferred_uploads.append(result)
        return results

    def write_bronze(
        self,
        frame: pd.DataFrame,
        region: Region,
        *,
        partition_date: date,
        backfill: bool = False,
        commit_message: str | None = None,
        upload: bool = True,
    ) -> WriteResult:
        """Write one parquet partition locally and land it in HF bronze."""
        if frame.empty:
            raise PermanentError(
                f"{self.source_id}/{region.region_id}: refusing to write an empty partition"
            )
        prefix = bronze_prefix(
            self.source_id,
            region_id=region.region_id,
            year=partition_date.year,
            month=partition_date.month,
            day=None if backfill else partition_date.day,
            backfill=backfill,
        )
        filename = bronze_filename(
            self.source_id,
            region.region_id,
            year=partition_date.year,
            month=partition_date.month,
            day=None if backfill else partition_date.day,
            backfill=backfill,
        )
        local_path = self.staging_dir / prefix / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)

        frame.to_parquet(local_path, index=False, compression="zstd")
        size = local_path.stat().st_size
        result = WriteResult(rows=len(frame), local_path=local_path, parquet_bytes=size)

        if self.uploader is None or self.dry_run or not upload:
            reason = "dry-run" if self.dry_run else ("no uploader" if self.uploader is None else "deferred")
            self.log(
                logging.INFO,
                f"{self.source_id}/{region.region_id}: wrote {len(frame)} row(s) locally "
                f"(upload skipped: {reason})",
                source_id=self.source_id,
                region_id=region.region_id,
                rows=len(frame),
            )
            return result

        path_in_repo = f"{prefix}/{filename}"
        info = self.uploader.upload_file(
            local_path,
            path_in_repo,
            commit_message=commit_message
            or f"bronze: {self.source_id} {region.region_id} {partition_date.isoformat()}",
        )
        result.path_in_repo = path_in_repo
        result.commit_sha = getattr(info, "oid", None) or getattr(info, "commit_sha", None)
        self.log(
            logging.INFO,
            f"{self.source_id}/{region.region_id}: landed {len(frame)} row(s) at {path_in_repo}",
            source_id=self.source_id,
            region_id=region.region_id,
            rows=len(frame),
            path_in_repo=path_in_repo,
        )
        return result

    # -- orchestration -----------------------------------------------------

    def collect_region(self, region: Region, **kwargs: Any) -> CollectionResult:
        """Run the full fetch->write pipeline for one region, never raising."""
        started = self.now()
        result = CollectionResult(
            source_id=self.source_id,
            region_id=region.region_id,
            status="failed",
            started_at=started.isoformat(),
        )
        backfill = bool(kwargs.pop("backfill", False))
        kwargs.pop("partition_date", None)  # partitions derive from observation time

        try:
            raw = self.fetch(region, backfill=backfill, **kwargs)
            raw = self.normalize(raw, region, **kwargs)

            # An empty landing is a legitimate outcome (no fire in a region this
            # week, polar night, quiet window) and has no shape to validate.
            if raw.empty:
                result.status = "empty"
                result.details["note"] = "source returned no rows for this region/window"
                self.log(
                    logging.WARNING,
                    f"{self.source_id}/{region.region_id}: no rows returned",
                    source_id=self.source_id,
                    region_id=region.region_id,
                )
                return self._finalize(result, started)

            raw = self.validate_raw(raw)
            enriched = self.validate_envelope(self.enrich(raw, region, observed_at=started))
            writes = self.write_partitions(enriched, region, backfill=backfill)

            result.status = "success"
            result.rows = sum(item.rows for item in writes)
            result.details["partition_count"] = len(writes)
            result.details["partitions"] = [item.as_dict() for item in writes]
            if writes:
                result.path_in_repo = writes[-1].path_in_repo
                result.local_path = (
                    str(writes[-1].local_path) if writes[-1].local_path else None
                )

        except CircuitBreakerOpen as exc:
            result.status = "circuit_open"
            result.error_type = type(exc).__name__
            result.error = redact_text(exc)
            self.log(
                logging.ERROR,
                f"{self.source_id}/{region.region_id}: circuit open, skipping: {exc}",
                source_id=self.source_id,
                region_id=region.region_id,
                status="circuit_open",
            )
        except Exception as exc:  # noqa: BLE001 - one region must not kill the run
            result.error_type = type(exc).__name__
            result.error = redact_text(exc)
            self.log(
                logging.ERROR,
                f"{self.source_id}/{region.region_id}: {type(exc).__name__}: {exc}",
                source_id=self.source_id,
                region_id=region.region_id,
                status="failed",
            )

        return self._finalize(result, started)

    def _finalize(self, result: CollectionResult, started: datetime) -> CollectionResult:
        finished = self.now()
        result.finished_at = finished.isoformat()
        result.duration_s = round((finished - started).total_seconds(), 3)
        result.requests_made = self.requests_made
        return result

    def _flush_deferred_uploads(self) -> None:
        """Land all staged backfill partitions in one Hub commit."""
        if not self._deferred_uploads or self.uploader is None:
            return
        folder = self.staging_dir / "backfill" / self.source_id
        if not folder.is_dir():
            return
        try:
            sha = self.uploader.upload_folder(  # type: ignore[attr-defined]
                folder,
                f"backfill/{self.source_id}",
                commit_message=f"backfill: {self.source_id} ({len(self._deferred_uploads)} partition(s))",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the summary, not fatal
            self.log(
                logging.ERROR,
                f"{self.source_id}: deferred backfill upload failed: {exc}",
                source_id=self.source_id,
                status="failed",
            )
            raise
        for result in self._deferred_uploads:
            result.commit_sha = str(sha)
        self.log(
            logging.INFO,
            f"{self.source_id}: landed {len(self._deferred_uploads)} backfill partition(s) "
            f"in one commit ({sha[:8]})",
            source_id=self.source_id,
            rows=sum(item.rows for item in self._deferred_uploads),
        )

    def run(self, regions: Sequence[Region] | None = None, **kwargs: Any) -> CollectionSummary:
        """Collect every requested region; the breaker short-circuits the rest."""
        targets = list(regions) if regions is not None else list(REGIONS)
        started = self.now()
        summary = CollectionSummary(
            source_id=self.source_id, started_at=started.isoformat(), finished_at="", duration_s=0.0
        )
        self.log(
            logging.INFO,
            f"{self.source_id}: collecting {len(targets)} region(s) with H3 res "
            f"{self.spec.h3_resolution}",
            source_id=self.source_id,
        )

        for region in targets:
            summary.results.append(self.collect_region(region, **kwargs))
            if self.breaker.state.value == "open":
                remaining = [r for r in targets if r.region_id not in {
                    item.region_id for item in summary.results
                }]
                for skipped in remaining:
                    summary.results.append(
                        CollectionResult(
                            source_id=self.source_id,
                            region_id=skipped.region_id,
                            status="circuit_open",
                            error_type="CircuitBreakerOpen",
                            error="skipped: circuit breaker opened during this run",
                        )
                    )
                break

        finished = self.now()
        summary.finished_at = finished.isoformat()
        summary.duration_s = round((finished - started).total_seconds(), 3)
        summary.breaker = self.breaker.snapshot()

        # Attempted even when some regions failed: landing the data that *did*
        # arrive is better than discarding a long fetch because one window was
        # flaky, and the summary already records which regions failed.
        try:
            self._flush_deferred_uploads()
        except Exception as exc:  # noqa: BLE001 - report, do not mask collection results
            summary.results.append(
                CollectionResult(
                    source_id=self.source_id,
                    region_id="__upload__",
                    status="failed",
                    error_type=type(exc).__name__,
                    error=redact_text(exc),
                )
            )

        self.log(
            logging.WARNING if summary.failed else logging.INFO,
            f"{self.source_id}: {len(summary.results)} region(s), {summary.total_rows} row(s), "
            f"{len(summary.failed)} failure(s) in {summary.duration_s}s",
            source_id=self.source_id,
            status="failed" if summary.failed else "ok",
            rows=summary.total_rows,
        )
        return summary


# --------------------------------------------------------------------------
# CLI helpers shared by every collector entrypoint
# --------------------------------------------------------------------------

def select_regions(
    anomaly_types: Iterable[str] | None,
    spec: str,
    *,
    default: Sequence[Region] = REGIONS,
) -> tuple[Region, ...]:
    """Resolve ``--regions`` input into Region objects.

    ``all`` (or an empty string) selects every region matching ``anomaly_types``
    when given, otherwise the supplied default set.
    """
    types = {str(value).strip().lower() for value in (anomaly_types or []) if str(value).strip()}
    if spec.strip().lower() in {"", "all"}:
        regions = tuple(
            region for region in default if not types or region.anomaly_type in types
        )
    else:
        regions = tuple(get_region(value.strip()) for value in spec.split(",") if value.strip())

    if not regions:
        raise ValueError(
            f"--regions={spec!r} selected nothing"
            + (f" for anomaly types {sorted(types)}" if types else "")
            + f". Known regions: {sorted(r.region_id for r in REGIONS)}"
        )
    return regions


def common_arg_parser(description: str, *, default_regions: str = "all") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--regions",
        default=default_regions,
        help="comma-separated region ids, or 'all' (default: %(default)s)",
    )
    parser.add_argument("--start-date", help="inclusive start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="inclusive end date (YYYY-MM-DD)")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="archive product selection and monthly partitions under backfill/",
    )
    parser.add_argument("--dry-run", action="store_true", help="write locally, skip HF upload")
    parser.add_argument("--run-id", default=None, help="override the generated run id")
    parser.add_argument(
        "--workflow",
        default=None,
        help="workflow name recorded in pipeline_runs (default: collect_<source>)",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="do not write a pipeline_runs row for this run",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "INFO"), help="logging level"
    )
    parser.add_argument(
        "--log-json", action="store_true", help="force JSON log output (default in GitHub Actions)"
    )
    return parser


def parse_date(value: str | None, *, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"--{field_name} must be YYYY-MM-DD, got {value!r}") from exc


def build_uploader(*, dry_run: bool) -> Uploader | None:
    """Construct the HF bronze uploader, or ``None`` when running dry."""
    if dry_run:
        return None
    from storage.hf_dataset_writer import HFDatasetWriter

    return HFDatasetWriter(repo_kind="bronze")


def print_summary(summary: CollectionSummary) -> None:
    print(json.dumps(summary.as_dict(), indent=2, default=str))


def record_run_metadata(
    *,
    workflow: str,
    summary: CollectionSummary,
    enabled: bool = True,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Write this run's outcome to ``pipeline_runs`` (HF bronze, and Turso if configured).

    Kept here rather than as YAML glue in a workflow step so the status mapping
    and redaction are unit-tested. Sink construction is individually guarded: a
    misconfigured database must not invalidate a collection run that succeeded.
    """
    if not enabled or dry_run:
        return None

    from ops.pipeline_run import PipelineRunRecorder

    hf_writer = None
    turso = None
    if get_secret("HF_TOKEN", required=False):
        try:
            from storage.hf_dataset_writer import HFDatasetWriter

            hf_writer = HFDatasetWriter(repo_kind="bronze")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("run metadata: HF sink unavailable: %s", redact_text(exc))
    if get_secret("TURSO_DATABASE_URL", required=False) and get_secret(
        "TURSO_AUTH_TOKEN", required=False
    ):
        try:
            from storage.turso_client import TursoClient

            turso = TursoClient()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("run metadata: Turso sink unavailable: %s", redact_text(exc))

    recorder = PipelineRunRecorder(
        workflow=workflow, source_id=summary.source_id, hf_writer=hf_writer, turso=turso
    )
    return recorder.record_summary(summary).as_dict()


def main_for(collector_cls: type[BaseCollector], argv: Sequence[str] | None = None) -> int:
    """Standard entrypoint body for a collector module's ``__main__``."""
    parser = common_arg_parser(collector_cls.__doc__ or collector_cls.label)
    args = parser.parse_args(argv)
    configure_logging(args.log_level, json_output=True if args.log_json else None)

    spec = SOURCES[collector_cls.source_id]
    if args.regions.strip().lower() in {"", "all"} and collector_cls.default_region_ids:
        regions = tuple(get_region(value) for value in collector_cls.default_region_ids)
    else:
        regions = select_regions(spec.anomaly_types, args.regions)

    collector = collector_cls(
        uploader=build_uploader(dry_run=args.dry_run),
        dry_run=args.dry_run,
        run_id=args.run_id,
    )
    summary = collector.run(
        regions,
        start_date=parse_date(args.start_date, field_name="start-date"),
        end_date=parse_date(args.end_date, field_name="end-date"),
        backfill=args.backfill,
    )
    run_record = record_run_metadata(
        workflow=args.workflow or f"collect_{collector_cls.source_id}",
        summary=summary,
        enabled=not args.no_metadata,
        dry_run=args.dry_run,
    )
    payload: dict[str, Any] = {"summary": summary.as_dict()}
    if run_record is not None:
        payload["pipeline_run"] = run_record
    print(json.dumps(payload, indent=2, default=str))
    return summary.exit_code()
