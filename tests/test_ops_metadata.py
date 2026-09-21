"""Run metadata and alerting — the workflow-facing behaviours.

Requirement: every workflow records what it did and pages a human when it breaks.
These tests pin the parts that are easy to get subtly wrong — status mapping,
secret redaction in an outbound payload, and the rule that alerting never turns a
successful run into a failed one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from collectors.base_collector import CollectionResult, CollectionSummary
from collectors.config import SOURCES
from ops.notify import build_payload, detect_channel, notify, notify_workflow_failure
from ops.pipeline_run import (
    PIPELINE_RUNS_DDL,
    PipelineRunRecorder,
    ensure_metadata_tables,
    register_sources,
    source_metadata_rows,
)
from ops.resilience import RetryPolicy
from tests.conftest import FakeUploader

FIXED_NOW = datetime(2024, 9, 16, 12, 0, 0, tzinfo=UTC)


class FakeTurso:
    """Stands in for TursoClient, recording DDL and upserts."""

    def __init__(self, *, fail: bool = False) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.upserts: list[tuple[str, list[dict[str, Any]], list[str]]] = []
        self.fail = fail

    def execute_many(self, statements: Any) -> list[dict[str, Any]]:
        self.statements.extend(statements)
        return [{"rows": [], "affected": 0} for _ in statements]

    def upsert(self, table: str, rows: Any, *, key_columns: Any, **kwargs: Any) -> int:
        if self.fail:
            raise RuntimeError("turso unreachable")
        materialised = [dict(row) for row in rows]
        self.upserts.append((table, materialised, list(key_columns)))
        return len(materialised)


def make_summary(*results: CollectionResult, breaker: dict[str, Any] | None = None) -> CollectionSummary:
    return CollectionSummary(
        source_id="firms",
        started_at="2024-09-16T11:59:30+00:00",
        finished_at=FIXED_NOW.isoformat(),
        duration_s=30.0,
        results=list(results),
        breaker=breaker or {"name": "firms", "state": "closed"},
    )


def make_recorder(uploader: FakeUploader, turso: Any, **overrides: Any) -> PipelineRunRecorder:
    defaults = dict(
        workflow="collect_data",
        source_id="firms",
        run_id="collect_data-20240916T120000-abcdef",
        hf_writer=uploader,
        turso=turso,
        now=lambda: FIXED_NOW,
        environ={},
    )
    defaults.update(overrides)
    return PipelineRunRecorder(**defaults)


class TestStatusMapping:
    def test_all_regions_succeeding_is_success(self) -> None:
        summary = make_summary(
            CollectionResult(source_id="firms", region_id="iberia_fire", status="success", rows=10),
            CollectionResult(source_id="firms", region_id="greece_fire", status="success", rows=4),
        )
        run = make_recorder(FakeUploader(), FakeTurso()).record_summary(summary)
        assert run.status == "success"
        assert run.rows_written == 14
        assert run.failed_units == 0

    def test_some_regions_failing_is_partial(self) -> None:
        summary = make_summary(
            CollectionResult(source_id="firms", region_id="iberia_fire", status="success", rows=10),
            CollectionResult(
                source_id="firms",
                region_id="greece_fire",
                status="failed",
                error="HTTP 500",
            ),
        )
        run = make_recorder(FakeUploader(), FakeTurso()).record_summary(summary)
        assert run.status == "partial"
        assert run.failed_units == 1
        assert "greece_fire" in (run.error or "")

    def test_every_region_failing_is_failed(self) -> None:
        summary = make_summary(
            CollectionResult(
                source_id="firms", region_id="iberia_fire", status="failed", error="boom"
            )
        )
        run = make_recorder(FakeUploader(), FakeTurso()).record_summary(summary)
        assert run.status == "failed"

    def test_empty_landings_count_as_ok(self) -> None:
        summary = make_summary(
            CollectionResult(source_id="firms", region_id="iberia_fire", status="empty")
        )
        run = make_recorder(FakeUploader(), FakeTurso()).record_summary(summary)
        assert run.status == "success"
        assert run.ok is True

    def test_breaker_state_is_recorded_in_details(self) -> None:
        summary = make_summary(
            CollectionResult(source_id="firms", region_id="iberia_fire", status="success", rows=1),
            breaker={"name": "firms", "state": "open", "failure_count": 5},
        )
        run = make_recorder(FakeUploader(), FakeTurso()).record_summary(summary)
        assert run.details["breaker"]["state"] == "open"

    def test_duration_is_computed_from_the_summary_start(self) -> None:
        summary = make_summary(
            CollectionResult(source_id="firms", region_id="iberia_fire", status="success", rows=1)
        )
        run = make_recorder(FakeUploader(), FakeTurso()).record_summary(summary)
        assert run.duration_s == 30.0


class TestSinks:
    def test_writes_a_json_manifest_to_hf_bronze(self) -> None:
        uploader = FakeUploader()
        recorder = make_recorder(uploader, FakeTurso())
        run = recorder.record(status="success", rows_written=9)

        assert uploader.paths == [
            f"ops/pipeline_runs/collect_data/{run.run_id}.json"
        ]
        payload = json.loads(uploader.files[uploader.paths[0]])
        assert payload["status"] == "success"
        assert payload["rows_written"] == 9

    def test_upserts_a_row_into_turso(self) -> None:
        turso = FakeTurso()
        recorder = make_recorder(FakeUploader(), turso)
        recorder.record(status="success", rows_written=9, details={"partitions": 2})

        table, rows, keys = turso.upserts[0]
        assert table == "pipeline_runs"
        assert keys == ["run_id"]
        assert rows[0]["rows_written"] == 9
        assert json.loads(rows[0]["details"]) == {"partitions": 2}

    def test_metadata_tables_are_created_idempotently(self) -> None:
        turso = FakeTurso()
        ensure_metadata_tables(turso)
        ensure_metadata_tables(turso)
        assert len(turso.statements) == 4  # 2 statements, run twice
        assert any("CREATE TABLE IF NOT EXISTS pipeline_runs" in sql for sql, _ in turso.statements)
        assert any(json.dumps(args) for _, args in turso.statements) is not None
        assert "CREATE TABLE" in PIPELINE_RUNS_DDL

    def test_hf_failure_does_not_break_the_record(self) -> None:
        uploader = FakeUploader()
        uploader.fail_with = RuntimeError("hub down")
        turso = FakeTurso()
        run = make_recorder(uploader, turso).record(status="success", rows_written=1)

        assert run.status == "success"
        assert turso.upserts  # the database copy still landed

    def test_turso_failure_does_not_break_the_record(self) -> None:
        uploader = FakeUploader()
        run = make_recorder(uploader, FakeTurso(fail=True)).record(status="success", rows_written=1)
        assert run.status == "success"
        assert uploader.paths  # the Hub copy still landed

    def test_recording_works_with_no_sinks_at_all(self) -> None:
        run = make_recorder(None, None).record(status="failed", error="boom")
        assert run.status == "failed"
        assert run.error == "boom"

    def test_github_context_is_captured(self) -> None:
        recorder = make_recorder(
            None,
            None,
            environ={
                "GITHUB_RUN_ID": "12345",
                "GITHUB_SHA": "abcdef1234567890",
                "GITHUB_ACTOR": "octocat",
                "GITHUB_REF_NAME": "main",
            },
        )
        run = recorder.record(status="success")
        assert run.github_run_id == "12345"
        assert run.github_sha == "abcdef1234567890"
        assert run.github_actor == "octocat"

    def test_errors_are_redacted_before_storage(self) -> None:
        uploader = FakeUploader()
        recorder = make_recorder(uploader, FakeTurso())
        run = recorder.record(
            status="failed",
            error="auth failed with token hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        )
        assert "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in (run.error or "")
        assert "***redacted***" in (run.error or "")


class TestSourceMetadata:
    def test_one_row_per_source_with_attribution(self) -> None:
        rows = source_metadata_rows()
        assert {row["source_id"] for row in rows} == set(SOURCES)
        assert all(row["attribution"] for row in rows)
        assert all(row["cadence_cron"] for row in rows)

    def test_credentials_are_reported_as_presence_only(self, monkeypatch) -> None:
        monkeypatch.setenv("FIRMS_MAP_KEY", "a-very-secret-value")
        rows = {row["source_id"]: row for row in source_metadata_rows()}
        assert json.loads(rows["firms"]["credentials"]) == {"FIRMS_MAP_KEY": "set"}
        assert "a-very-secret-value" not in json.dumps(rows)

    def test_register_sources_upserts_by_source_id(self) -> None:
        turso = FakeTurso()
        count = register_sources(turso)
        assert count == len(SOURCES)
        table, rows, keys = turso.upserts[0]
        assert table == "sources"
        assert keys == ["source_id"]
        assert len(rows) == len(SOURCES)


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

class FakeWebhookResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = "ok"


class FakeWebhookSession:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json: Any = None, timeout: Any = None) -> Any:
        self.calls.append((url, json))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestChannelDetection:
    def test_detects_slack(self) -> None:
        assert detect_channel("https://hooks.slack.com/services/T/B/C") == "slack"

    def test_detects_discord(self) -> None:
        assert detect_channel("https://discord.com/api/webhooks/1/abc") == "discord"

    def test_falls_back_to_generic(self) -> None:
        assert detect_channel("https://example.test/hook") == "generic"


class TestBuildPayload:
    def test_slack_uses_text_field(self) -> None:
        payload = build_payload("slack", "pipeline failed", level="error", context={"workflow": "x"})
        assert "pipeline failed" in payload["text"]
        assert "workflow" in payload["text"]

    def test_discord_uses_content_field(self) -> None:
        payload = build_payload("discord", "pipeline failed")
        assert "content" in payload

    def test_payload_bodies_are_redacted(self) -> None:
        payload = build_payload(
            "slack",
            "bad key",
            context={"detail": "token hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345"},
        )
        assert "hf_AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in json.dumps(payload)

    def test_credential_keys_report_presence(self) -> None:
        payload = build_payload("slack", "hi", context={"HF_TOKEN": "hf_secret_value_123456"})
        assert "set" in payload["text"]


class TestNotify:
    def test_missing_webhook_is_reported_not_raised(self, monkeypatch) -> None:
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        result = notify("boom")
        assert result.delivered is False
        assert result.skipped_reason == "no webhook configured"

    def test_successful_delivery(self) -> None:
        session = FakeWebhookSession(FakeWebhookResponse(200))
        result = notify("boom", webhook_url="https://hooks.slack.com/services/T/B/C", session=session)
        assert result.delivered is True
        assert result.channel == "slack"

    def test_delivery_failure_does_not_raise(self) -> None:
        import requests

        session = FakeWebhookSession(requests.ConnectionError("no route"))
        result = notify(
            "boom",
            webhook_url="https://discord.com/api/webhooks/1/abc",
            session=session,
            sleep=lambda _s: None,
        )
        assert result.delivered is False
        assert result.error

    def test_transient_failure_is_retried(self) -> None:
        session = FakeWebhookSession(FakeWebhookResponse(503), FakeWebhookResponse(200))
        result = notify(
            "boom",
            webhook_url="https://hooks.slack.com/services/T/B/C",
            session=session,
            policy=RetryPolicy(attempts=2, base_delay=0.01, jitter="none"),
            sleep=lambda _s: None,
        )
        assert result.delivered is True
        assert len(session.calls) == 2

    def test_client_error_is_not_retried(self) -> None:
        session = FakeWebhookSession(FakeWebhookResponse(404))
        result = notify(
            "boom",
            webhook_url="https://hooks.slack.com/services/T/B/C",
            session=session,
            policy=RetryPolicy(attempts=3, base_delay=0.01, jitter="none"),
            sleep=lambda _s: None,
        )
        assert result.delivered is False
        assert len(session.calls) == 1

    def test_workflow_failure_enriches_context_from_actions_env(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_WORKFLOW", "collect_data")
        monkeypatch.setenv("GITHUB_RUN_ID", "999")
        monkeypatch.setenv("GITHUB_REPOSITORY", "demo-org/TerraSentinel")
        monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

        result = notify_workflow_failure(session=FakeWebhookSession())
        assert result.delivered is False
        assert result.skipped_reason == "no webhook configured"

    def test_extra_context_is_redacted_in_the_body(self) -> None:
        session = FakeWebhookSession(FakeWebhookResponse(200))
        notify_workflow_failure(
            message="collector died",
            extra_context={"FIRMS_MAP_KEY": "super-secret-key-value"},
            webhook_url="https://hooks.slack.com/services/T/B/C",
            session=session,
        )
        body = json.dumps(session.calls[0][1])
        assert "super-secret-key-value" not in body
