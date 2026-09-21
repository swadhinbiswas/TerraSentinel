"""Run metadata: every workflow records what it did, into Turso and HF bronze.

Two readers depend on this table:

* the dashboard's "last updated" indicator and ``/api/health``, which answer
  "is the pipeline healthy" from data rather than from uptime ping;
* an operator asking "did last night's 03:00 Sentinel run land anything, and if
  not, which region failed".

Recording is deliberately best-effort: a metadata write must never turn a
successful collection run into a failed one, but it does log loudly when it fails.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from collectors.config import SOURCES, secret_status
from ops.redact import redact_mapping, redact_text
from storage.turso_client import TursoClient

LOGGER = logging.getLogger(__name__)

__all__ = [
    "PIPELINE_RUNS_DDL",
    "SOURCES_DDL",
    "PipelineRun",
    "PipelineRunRecorder",
    "ensure_metadata_tables",
    "register_sources",
    "source_metadata_rows",
]

PIPELINE_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id       TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    source_id    TEXT,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    duration_s   REAL,
    rows_written INTEGER NOT NULL DEFAULT 0,
    failed_units INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    github_run_id TEXT,
    github_sha    TEXT,
    github_actor  TEXT,
    github_ref    TEXT,
    details      TEXT,
    updated_at   TEXT NOT NULL
)
""".strip()

SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    metric_types  TEXT,
    anomaly_types TEXT,
    h3_resolution INTEGER,
    cadence_cron  TEXT,
    cadence_human TEXT,
    attribution   TEXT,
    docs_url      TEXT,
    notes         TEXT,
    credentials   TEXT,
    updated_at    TEXT NOT NULL
)
""".strip()


def _utc_now() -> datetime:

    return datetime.now(UTC)


@dataclass(slots=True)
class PipelineRun:
    """One workflow execution, as stored in ``pipeline_runs``."""

    run_id: str
    workflow: str
    status: str
    started_at: str
    source_id: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    rows_written: int = 0
    failed_units: int = 0
    error: str | None = None
    github_run_id: str | None = None
    github_sha: str | None = None
    github_actor: str | None = None
    github_ref: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"success", "empty"}

    def as_row(self, *, updated_at: str) -> dict[str, Any]:
        row = asdict(self)
        details = row.pop("details") or {}
        row["details"] = json.dumps(details, default=str)
        row["updated_at"] = updated_at
        return row

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def source_metadata_rows(*, updated_at: str | None = None) -> list[dict[str, Any]]:
    """One row per source: cadence, attribution, and credential *presence* only.

    This is what the dashboard footer and the health view read, so attribution
    text lives in one place instead of being retyped into HTML.
    """
    stamp = updated_at or _utc_now().isoformat()
    rows: list[dict[str, Any]] = []
    for spec in SOURCES.values():
        rows.append(
            {
                "source_id": spec.source_id,
                "label": spec.label,
                "metric_types": json.dumps(list(spec.metric_types)),
                "anomaly_types": json.dumps(list(spec.anomaly_types)),
                "h3_resolution": spec.h3_resolution,
                "cadence_cron": spec.cadence_cron,
                "cadence_human": spec.cadence_human,
                "attribution": spec.attribution,
                "docs_url": spec.docs_url,
                "notes": spec.notes,
                "credentials": json.dumps(secret_status(spec.required_secrets)),
                "updated_at": stamp,
            }
        )
    return rows


def ensure_metadata_tables(client: TursoClient) -> None:
    """Create the metadata tables if absent. Idempotent DDL, safe on every run."""
    client.execute_many([(PIPELINE_RUNS_DDL, ()), (SOURCES_DDL, ())])


def register_sources(client: TursoClient) -> int:
    """Upsert the source registry so attribution and cadence are queryable."""
    ensure_metadata_tables(client)
    rows = source_metadata_rows()
    return client.upsert("sources", rows, key_columns=["source_id"])


def new_run_id(workflow: str) -> str:
    """Unique per execution: workflow + timestamp + short random suffix."""
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S")
    return f"{workflow}-{stamp}-{uuid.uuid4().hex[:6]}"


class PipelineRunRecorder:
    """Accumulates one run's outcome and writes it to HF bronze and Turso.

    Both sinks are optional and independently failure-isolated, so a Hub outage
    does not stop the run record from reaching the database the dashboard reads.
    """

    def __init__(
        self,
        *,
        workflow: str,
        source_id: str | None = None,
        run_id: str | None = None,
        hf_writer: Any | None = None,
        turso: TursoClient | None = None,
        now: Callable[[], datetime] = _utc_now,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.workflow = workflow
        self.source_id = source_id
        self.run_id = run_id or new_run_id(workflow)
        self.hf_writer = hf_writer
        self.turso = turso
        self._now = now
        env = os.environ if environ is None else environ
        self._started_at = self._now().isoformat()
        self._github = {
            "github_run_id": env.get("GITHUB_RUN_ID"),
            "github_sha": (env.get("GITHUB_SHA") or "")[:40] or None,
            "github_actor": env.get("GITHUB_ACTOR"),
            "github_ref": env.get("GITHUB_REF_NAME"),
        }

    # -- recording ---------------------------------------------------------

    def record(
        self,
        *,
        status: str,
        rows_written: int = 0,
        failed_units: int = 0,
        error: str | None = None,
        details: Mapping[str, Any] | None = None,
        started_at: str | None = None,
    ) -> PipelineRun:
        finished = self._now()
        started_at_value = started_at or self._started_at
        try:
            duration = (finished - datetime.fromisoformat(started_at_value)).total_seconds()
        except ValueError:
            duration = None

        run = PipelineRun(
            run_id=self.run_id,
            workflow=self.workflow,
            status=status,
            started_at=started_at_value,
            source_id=self.source_id,
            finished_at=finished.isoformat(),
            duration_s=round(duration, 3) if duration is not None else None,
            rows_written=int(rows_written),
            failed_units=int(failed_units),
            error=redact_text(error) if error else None,
            details=redact_mapping(details or {}),
            **self._github,
        )

        self._write_hf(run)
        self._write_turso(run)
        return run

    def record_summary(self, summary: Any) -> PipelineRun:
        """Record a ``CollectionSummary`` (duck-typed to avoid a layering import)."""
        results = list(getattr(summary, "results", []) or [])
        failed = [item for item in results if not getattr(item, "ok", False)]
        status = "failed" if results and len(failed) == len(results) else ("partial" if failed else "success")
        region_rows = {
            getattr(item, "region_id", "unknown"): {
                "status": getattr(item, "status", None),
                "rows": getattr(item, "rows", 0),
                "error": getattr(item, "error", None),
                "path": getattr(item, "path_in_repo", None),
            }
            for item in results
        }
        return self.record(
            status=status,
            rows_written=int(getattr(summary, "total_rows", 0) or 0),
            failed_units=len(failed),
            error="; ".join(
                f"{getattr(item, 'region_id', '?')}: {getattr(item, 'error', None)}"
                for item in failed
            )
            or None,
            details={
                "regions": region_rows,
                "breaker": getattr(summary, "breaker", None),
                "duration_s": getattr(summary, "duration_s", None),
            },
            started_at=getattr(summary, "started_at", None),
        )

    def fail(self, error: BaseException | str, **kwargs: Any) -> PipelineRun:
        return self.record(status="failed", error=str(error), **kwargs)

    # -- sinks -------------------------------------------------------------

    def _write_hf(self, run: PipelineRun) -> None:
        if self.hf_writer is None:
            return
        try:
            self.hf_writer.upload_json(
                run.as_dict(),
                f"ops/pipeline_runs/{self.workflow}/{run.run_id}.json",
                commit_message=f"pipeline_runs: {self.workflow} {run.status} ({run.run_id})",
            )
        except Exception as exc:  # noqa: BLE001 - metadata must not break the run
            LOGGER.error("could not write run record to HF: %s", redact_text(exc))

    def _write_turso(self, run: PipelineRun) -> None:
        if self.turso is None:
            return
        try:
            ensure_metadata_tables(self.turso)
            self.turso.upsert(
                "pipeline_runs",
                [run.as_row(updated_at=self._now().isoformat())],
                key_columns=["run_id"],
            )
            # Refreshed here rather than only in the metadata CLI, because this is the path
            # every workflow actually takes — without it the source registry stayed empty
            # and the dashboard's attribution had nothing to read.
            register_sources(self.turso)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("could not write run record to Turso: %s", redact_text(exc))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record a TerraSentinel pipeline run in Turso and/or HF bronze."
    )
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--status", required=True, choices=["success", "partial", "failed", "empty"])
    parser.add_argument("--rows", type=int, default=0)
    parser.add_argument("--failed-units", type=int, default=0)
    parser.add_argument("--error", default=None)
    parser.add_argument("--details", default=None, help="JSON object")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--skip-hf", action="store_true")
    parser.add_argument("--skip-turso", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    details: dict[str, Any] = {}
    if args.details:
        try:
            details = json.loads(args.details)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--details must be valid JSON: {exc}") from exc

    hf_writer = None
    if not args.skip_hf:
        from storage.hf_dataset_writer import HFDatasetWriter

        hf_writer = HFDatasetWriter(repo_kind="bronze")

    turso = None
    if not args.skip_turso:
        from collectors.config import get_secret

        if get_secret("TURSO_DATABASE_URL", required=False) and get_secret(
            "TURSO_AUTH_TOKEN", required=False
        ):
            turso = TursoClient()
            register_sources(turso)

    recorder = PipelineRunRecorder(
        workflow=args.workflow,
        source_id=args.source_id,
        run_id=args.run_id,
        hf_writer=hf_writer,
        turso=turso,
    )
    run = recorder.record(
        status=args.status,
        rows_written=args.rows,
        failed_units=args.failed_units,
        error=args.error,
        details=details,
    )
    print(json.dumps(run.as_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
