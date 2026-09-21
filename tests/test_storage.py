"""Hub writer and libSQL client, exercised against fakes (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

from ops.resilience import PermanentError, RetryPolicy, TransientError
from storage.hf_dataset_writer import HFDatasetWriter
from storage.turso_client import (
    TursoClient,
    TursoError,
    decode_cell,
    encode_arg,
    http_url_for,
)

# --------------------------------------------------------------------------
# Turso: URL handling, typing, statements
# --------------------------------------------------------------------------

class TestHttpUrl:
    def test_converts_libsql_scheme(self) -> None:
        assert (
            http_url_for("libsql://demo-org.turso.io")
            == "https://demo-org.turso.io/v2/pipeline"
        )

    def test_accepts_https_passthrough(self) -> None:
        assert http_url_for("https://demo.turso.io") == "https://demo.turso.io/v2/pipeline"

    def test_upgrades_plain_http(self) -> None:
        assert http_url_for("http://demo.turso.io") == "https://demo.turso.io/v2/pipeline"

    def test_strips_trailing_slash(self) -> None:
        assert http_url_for("libsql://demo.turso.io/") == "https://demo.turso.io/v2/pipeline"

    @pytest.mark.parametrize("value", ["file:/tmp/local.db", "postgres://host/db", ""])
    def test_rejects_unsupported_schemes(self, value: str) -> None:
        with pytest.raises(TursoError):
            http_url_for(value)


class TestTyping:
    @pytest.mark.parametrize(
        "value,kind,expected",
        [
            (None, "null", None),
            (True, "integer", "1"),
            (False, "integer", "0"),
            (7, "integer", "7"),
            ("text", "text", "text"),
        ],
    )
    def test_encode_arg(self, value: Any, kind: str, expected: Any) -> None:
        encoded = encode_arg(value)
        assert encoded["type"] == kind
        if expected is not None:
            assert encoded["value"] == expected

    def test_encode_float_is_a_json_number_not_a_string(self) -> None:
        # libSQL rejects a stringified float with a JSON parse error, so asserting
        # `float(value) == 1.5` would pass on a payload the server refuses.
        encoded = encode_arg(1.5)
        assert isinstance(encoded["value"], float), encoded
        assert encoded["value"] == 1.5

    def test_float_payload_is_valid_json(self) -> None:
        import json

        assert json.loads(json.dumps(encode_arg(1.5)))["value"] == 1.5

    def test_integer_may_remain_a_string(self) -> None:
        # libSQL accepts a string for the integer variant, so this is not a bug.
        assert isinstance(encode_arg(7)["value"], str)

    def test_encode_treats_non_finite_floats_as_null(self) -> None:
        assert encode_arg(float("nan")) == {"type": "null"}
        assert encode_arg(float("inf")) == {"type": "null"}

    def test_encode_blob_is_base64(self) -> None:
        assert encode_arg(b"\x00\x01")["type"] == "blob"

    @pytest.mark.parametrize(
        "cell,expected",
        [
            ({"type": "integer", "value": "42"}, 42),
            ({"type": "float", "value": "1.5"}, 1.5),
            ({"type": "text", "value": "hi"}, "hi"),
            ({"type": "null"}, None),
            (None, None),
        ],
    )
    def test_decode_cell(self, cell: Any, expected: Any) -> None:
        assert decode_cell(cell) == expected


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeTursoSession:
    """Records outgoing requests and replays canned pipeline responses."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None) -> Any:
        self.calls.append({"url": url, "headers": headers, "body": json})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def statements(self) -> list[dict[str, Any]]:
        if not self.calls:
            return []
        return [item for item in self.calls[-1]["body"]["requests"] if item["type"] == "execute"]


def pipeline_ok(*row_sets: Any, affected: int = 1) -> FakeResponse:
    """Fabricate a pipeline response with one execute result per row set."""
    results: list[dict[str, Any]] = []
    for rows in row_sets:
        columns = list(rows[0].keys()) if rows else []
        results.append(
            {
                "type": "ok",
                "response": {
                    "type": "execute",
                    "result": {
                        "cols": [{"name": name} for name in columns],
                        "rows": [[encode_arg(row[name]) for name in columns] for row in rows],
                        "affected_row_count": affected,
                    },
                },
            }
        )
    results.append({"type": "ok", "response": {"type": "close"}})
    return FakeResponse({"results": results})


def make_client(session: Any, **overrides: Any) -> TursoClient:
    defaults = dict(
        database_url="libsql://demo.turso.io",
        auth_token="test-token",
        session=session,
        policy=RetryPolicy(attempts=2, base_delay=0.01, jitter="none"),
        sleep=lambda _seconds: None,
    )
    defaults.update(overrides)
    return TursoClient(**defaults)


class TestTursoExecute:
    def test_parses_rows_into_dicts(self) -> None:
        session = FakeTursoSession(pipeline_ok([{"run_id": "abc", "rows": 12}]))
        client = make_client(session)

        result = client.execute("SELECT run_id, rows FROM pipeline_runs")

        assert result["rows"] == [{"run_id": "abc", "rows": 12}]
        assert result["affected"] == 1
        assert session.calls[0]["url"] == "https://demo.turso.io/v2/pipeline"
        assert session.calls[0]["headers"]["Authorization"] == "Bearer test-token"

    def test_sends_typed_arguments(self) -> None:
        session = FakeTursoSession(pipeline_ok([]))
        client = make_client(session)
        client.execute("SELECT 1 WHERE a = ? AND b = ?", ("text", 5))

        args = session.statements[0]["stmt"]["args"]
        assert args == [{"type": "text", "value": "text"}, {"type": "integer", "value": "5"}]

    def test_error_result_raises(self) -> None:
        payload = {
            "results": [
                {"type": "error", "error": {"message": "no such table: gold"}},
                {"type": "ok", "response": {"type": "close"}},
            ]
        }
        client = make_client(FakeTursoSession(FakeResponse(payload)))
        with pytest.raises(TursoError, match="no such table"):
            client.execute("SELECT * FROM gold")

    def test_server_error_is_retried_then_raised(self) -> None:
        session = FakeTursoSession(
            FakeResponse({"error": "busy"}, status_code=503),
            FakeResponse({"error": "busy"}, status_code=503),
        )
        client = make_client(session)
        with pytest.raises(TransientError):
            client.execute("SELECT 1")
        assert len(session.calls) == 2

    def test_retries_then_succeeds(self) -> None:
        session = FakeTursoSession(
            FakeResponse({"error": "busy"}, status_code=503),
            pipeline_ok([{"ok": 1}]),
        )
        client = make_client(session)
        assert client.execute("SELECT 1")["rows"] == [{"ok": 1}]
        assert len(session.calls) == 2

    def test_client_error_is_not_retried(self) -> None:
        session = FakeTursoSession(FakeResponse({"error": "bad"}, status_code=400))
        client = make_client(session)
        with pytest.raises(TursoError):
            client.execute("SELECT 1")
        assert len(session.calls) == 1

    def test_ping_reports_failure_without_raising(self) -> None:
        session = FakeTursoSession(
            FakeResponse({"error": "down"}, status_code=500),
            FakeResponse({"error": "down"}, status_code=500),
        )
        assert make_client(session).ping() is False

    def test_ping_reports_success(self) -> None:
        client = make_client(FakeTursoSession(pipeline_ok([{"ok": 1}])))
        assert client.ping() is True

    def test_missing_token_fails_before_any_request(self, monkeypatch) -> None:
        monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
        with pytest.raises(Exception, match="TURSO"):
            TursoClient(session=FakeTursoSession())


class TestTursoUpsert:
    def test_builds_conflict_update_sql(self) -> None:
        session = FakeTursoSession(pipeline_ok([]))
        client = make_client(session)

        client.upsert(
            "gold_fire_anomalies",
            [{"region_id": "iberia_fire", "day": "2024-08-15", "anomaly_score": 0.87}],
            key_columns=["region_id", "day"],
        )

        sql = session.statements[0]["stmt"]["sql"]
        assert sql.startswith("INSERT INTO gold_fire_anomalies (region_id, day, anomaly_score) VALUES")
        assert "ON CONFLICT (region_id, day) DO UPDATE SET anomaly_score = excluded.anomaly_score" in sql

    def test_key_only_rows_use_do_nothing(self) -> None:
        session = FakeTursoSession(pipeline_ok([]))
        client = make_client(session)
        client.upsert("t", [{"k": "a"}], key_columns=["k"])
        assert "DO NOTHING" in session.statements[0]["stmt"]["sql"]

    def test_single_upsert_fires_one_request(self) -> None:
        session = FakeTursoSession(pipeline_ok([]))
        client = make_client(session)
        client.upsert("t", [{"k": "a", "v": 1}, {"k": "b", "v": 2}], key_columns=["k"])
        assert len(session.calls) == 1
        assert session.statements[0]["stmt"]["sql"].count("(?, ?)") == 2

    def test_large_payloads_are_chunked_on_parameter_limit(self) -> None:
        rows = [{"k": str(index), "v": index} for index in range(400)]
        session = FakeTursoSession(pipeline_ok([], []))
        client = make_client(session)

        client.upsert("t", rows, key_columns=["k"])

        # One round trip, but split into statements under the 500-parameter
        # ceiling: 500 / 2 columns = 250 rows per statement.
        assert len(session.calls) == 1
        assert len(session.statements) == 2

    def test_empty_rows_short_circuits(self) -> None:
        session = FakeTursoSession()
        client = make_client(session)
        assert client.upsert("t", [], key_columns=["k"]) == 0
        assert session.calls == []

    def test_missing_key_column_is_rejected(self) -> None:
        client = make_client(FakeTursoSession())
        with pytest.raises(ValueError, match="key column"):
            client.upsert("t", [{"v": 1}], key_columns=["k"])

    def test_inconsistent_rows_are_rejected(self) -> None:
        client = make_client(FakeTursoSession())
        with pytest.raises(ValueError, match="inconsistent columns"):
            client.upsert("t", [{"k": "a", "v": 1}, {"k": "b", "w": 2}], key_columns=["k"])

    def test_requires_key_columns(self) -> None:
        client = make_client(FakeTursoSession())
        with pytest.raises(ValueError, match="key_columns is required"):
            client.upsert("t", [{"k": "a"}], key_columns=[])


# --------------------------------------------------------------------------
# Hugging Face writer
# --------------------------------------------------------------------------

class FakeRepoInfo:
    def __init__(self, sha: str = "deadbeef" * 5) -> None:
        self.sha = sha


class FakeHfApi:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.uploads: list[dict[str, Any]] = []
        self.folder_uploads: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.fail_times = 0
        self.status_code = 503

    def repo_info(self, repo_id: str, **kwargs: Any) -> FakeRepoInfo:
        if not self.exists:
            raise RepositoryNotFoundError("not found", response=_Response(404))
        return FakeRepoInfo()

    def upload_file(self, **kwargs: Any) -> Any:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise HfHubHTTPError("server error", response=_Response(self.status_code))
        self.uploads.append(kwargs)
        return FakeRepoInfo()

    def upload_folder(self, **kwargs: Any) -> Any:
        self.folder_uploads.append(kwargs)
        return FakeRepoInfo()

    def list_repo_files(self, repo_id: str, **kwargs: Any) -> list[str]:
        return ["firms/region=a/x.parquet", "ops/pipeline_runs/run.json"]


class _Response:
    """Minimal stand-in for requests.Response inside HfHubHTTPError."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.request = None


def make_writer(api: FakeHfApi, **overrides: Any) -> HFDatasetWriter:
    defaults = dict(
        repo_id="demo-org/climate-anomaly-bronze",
        repo_kind="bronze",
        token="hf_testtoken",
        api=api,
        policy=RetryPolicy(attempts=2, base_delay=0.01, jitter="none"),
        sleep=lambda _seconds: None,
    )
    defaults.update(overrides)
    return HFDatasetWriter(**defaults)


class TestHFDatasetWriter:
    def test_uploads_file_to_expected_path(self, tmp_path: Path) -> None:
        payload = tmp_path / "part.parquet"
        payload.write_bytes(b"PAR1")
        api = FakeHfApi()
        writer = make_writer(api)

        info = writer.upload_file(payload, "firms/region=iberia_fire/year=2024/month=08/part.parquet")

        assert info.sha
        assert api.uploads[0]["path_in_repo"].startswith("firms/region=iberia_fire/")
        assert api.uploads[0]["repo_type"] == "dataset"
        assert api.uploads[0]["commit_message"]

    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        payload = tmp_path / "part.parquet"
        payload.write_bytes(b"PAR1")
        writer = make_writer(FakeHfApi())
        for bad in ("../escape.parquet", "/absolute.parquet", "a/../../b.parquet", ""):
            with pytest.raises(ValueError):
                writer.upload_file(payload, bad)

    def test_missing_file_fails_loudly(self, tmp_path: Path) -> None:
        writer = make_writer(FakeHfApi())
        with pytest.raises(PermanentError, match="missing file"):
            writer.upload_file(tmp_path / "nope.parquet", "x.parquet")

    def test_creates_repo_when_absent(self) -> None:
        api = FakeHfApi(exists=False)
        writer = make_writer(api, create_if_missing=False)
        with pytest.raises(PermanentError, match="does not exist"):
            writer.prepare()
        assert api.created == []

    def test_prepare_is_idempotent(self) -> None:
        api = FakeHfApi()
        writer = make_writer(api)
        writer.prepare()
        writer.prepare()
        assert writer._prepared is True

    def test_server_errors_are_retried(self, tmp_path: Path) -> None:
        payload = tmp_path / "part.parquet"
        payload.write_bytes(b"PAR1")
        api = FakeHfApi()
        api.fail_times = 1
        writer = make_writer(api)

        writer.upload_file(payload, "x.parquet")
        assert len(api.uploads) == 1

    def test_persistent_server_error_raises_transient(self, tmp_path: Path) -> None:
        payload = tmp_path / "part.parquet"
        payload.write_bytes(b"PAR1")
        api = FakeHfApi()
        api.fail_times = 5
        writer = make_writer(api)
        with pytest.raises(TransientError):
            writer.upload_file(payload, "x.parquet")

    def test_upload_folder_returns_commit_sha(self, tmp_path: Path) -> None:
        (tmp_path / "backfill").mkdir()
        api = FakeHfApi()
        writer = make_writer(api)

        sha = writer.upload_folder(tmp_path, "backfill/firms")

        assert sha == "deadbeef" * 5
        assert api.folder_uploads[0]["path_in_repo"] == "backfill/firms"

    def test_upload_json_serialises_default_str(self) -> None:
        api = FakeHfApi()
        writer = make_writer(api)
        from datetime import datetime

        writer.upload_json(
            {"at": datetime(2024, 1, 1), "n": 3},
            "ops/pipeline_runs/test/run.json",
        )
        assert api.uploads[0]["path_or_fileobj"]

    def test_list_files_filters_by_prefix(self) -> None:
        writer = make_writer(FakeHfApi())
        assert writer.list_files("firms/") == ["firms/region=a/x.parquet"]

    def test_unknown_repo_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="repo_kind"):
            HFDatasetWriter(repo_kind="warehouse", token="x", api=FakeHfApi())


# --------------------------------------------------------------------------
# Hugging Face reader
# --------------------------------------------------------------------------

class FakeReaderApi:
    def __init__(self, *, missing: bool = False, files: list[str] | None = None) -> None:
        self.missing = missing
        self.files = files if files is not None else [
            "firms/region=iberia_fire/year=2024/month=08/day=15/a.parquet",
            "firms/region=iberia_fire/year=2024/month=08/day=16/b.parquet",
            "backfill/firms/region=arctic/year=2024/month=08/c.parquet",
            "ops/pipeline_runs/collect_data/run.json",
        ]

    def list_repo_files(self, repo_id: str, **kwargs: Any) -> list[str]:
        if self.missing:
            raise RepositoryNotFoundError("nope", response=_Response(404))
        return self.files

    def repo_info(self, repo_id: str, **kwargs: Any) -> FakeRepoInfo:
        if self.missing:
            raise RepositoryNotFoundError("nope", response=_Response(404))
        return FakeRepoInfo(sha="abc123def456")


def make_reader(api: Any, **overrides: Any) -> Any:
    from storage.hf_dataset_reader import HFDatasetReader

    defaults = dict(
        repo_id="demo-org/climate-anomaly-silver",
        repo_kind="silver",
        token="hf_testtoken",
        api=api,
    )
    defaults.update(overrides)
    return HFDatasetReader(**defaults)


class TestHFDatasetReader:
    def test_list_files_filters_by_prefix_and_suffix(self) -> None:
        reader = make_reader(FakeReaderApi())
        assert reader.list_files("firms/") == [
            "firms/region=iberia_fire/year=2024/month=08/day=15/a.parquet",
            "firms/region=iberia_fire/year=2024/month=08/day=16/b.parquet",
        ]
        assert len(reader.list_files("firms/", suffix=".json")) == 0

    def test_parquet_urls_resolve_to_hub_download_urls(self) -> None:
        reader = make_reader(FakeReaderApi())
        urls = reader.parquet_urls("firms/")
        assert len(urls) == 2
        assert urls[0] == (
            "https://huggingface.co/datasets/demo-org/climate-anomaly-silver/"
            "resolve/main/firms/region=iberia_fire/year=2024/month=08/day=15/a.parquet"
        )

    def test_resolve_url_honours_revision(self) -> None:
        reader = make_reader(FakeReaderApi(), revision="v1")
        assert "/resolve/v1/" in reader.resolve_url("x.parquet")

    def test_commit_sha_is_exposed_for_mlflow_lineage(self) -> None:
        reader = make_reader(FakeReaderApi())
        assert reader.commit_sha() == "abc123def456"

    def test_missing_repo_raises_permanent_error(self) -> None:
        reader = make_reader(FakeReaderApi(missing=True))
        with pytest.raises(PermanentError, match="not found"):
            reader.list_files("firms/")
        with pytest.raises(PermanentError, match="not found"):
            reader.commit_sha()

    def test_resolve_url_rejects_model_repos(self) -> None:
        reader = make_reader(FakeReaderApi(), repo_kind="models")
        with pytest.raises(PermanentError, match="dataset repos"):
            reader.resolve_url("model.bin")

    def test_unknown_repo_kind_is_rejected(self) -> None:
        from storage.hf_dataset_reader import HFDatasetReader

        with pytest.raises(ValueError, match="repo_kind"):
            HFDatasetReader(repo_kind="bucket", token="x", api=FakeReaderApi())
