"""Mirroring the bronze lake locally.

This is the fix for the transform's rate-limit problem: DuckDB re-lists the Hub
repo tree on every query touching an hf:// path, so a build made dozens of API
calls against a 1000-per-5-minutes quota. One mirror turns that into a single
listing plus parallel fetches, after which the build is entirely local.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ops.resilience import RetryPolicy, TransientError
from tools.sync_bronze import main, mirror_size, sync_bronze


class FakeReader:
    """Stands in for HFDatasetReader, writing a fake lake instead of downloading."""

    def __init__(self, *, files: int = 3, fail_times: int = 0, size: int = 100) -> None:
        self.files = files
        self.fail_times = fail_times
        self.size = size
        self.calls: list[dict[str, Any]] = []

    def snapshot(self, local_dir, *, allow_patterns=None, revision=None) -> Path:
        self.calls.append(
            {"local_dir": str(local_dir), "allow_patterns": allow_patterns, "revision": revision}
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientError("rate limited")
        target = Path(local_dir)
        for index in range(self.files):
            path = target / "backfill" / "firms" / f"part-{index}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * self.size)
        return target


def policy() -> RetryPolicy:
    return RetryPolicy(attempts=3, base_delay=0.01, max_delay=0.01)


class TestMirrorSize:
    def test_counts_lake_files_and_bytes(self, tmp_path: Path) -> None:
        (tmp_path / "firms").mkdir()
        (tmp_path / "firms" / "a.parquet").write_bytes(b"12345")
        assert mirror_size(tmp_path) == (1, 5)

    def test_ignores_the_client_metadata_cache(self, tmp_path: Path) -> None:
        # `.cache/` is huggingface_hub bookkeeping, not lake content; counting it
        # would overstate the lake and make "how big is it" unanswerable.
        (tmp_path / ".cache" / "huggingface").mkdir(parents=True)
        (tmp_path / ".cache" / "huggingface" / "meta").write_bytes(b"z" * 500)
        (tmp_path / "firms").mkdir()
        (tmp_path / "firms" / "a.parquet").write_bytes(b"12345")
        assert mirror_size(tmp_path) == (1, 5)

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert mirror_size(tmp_path) == (0, 0)


class TestSyncBronze:
    def test_downloads_and_reports(self, tmp_path: Path) -> None:
        reader = FakeReader(files=4, size=250)
        report = sync_bronze(
            local_dir=tmp_path / "lake", repo_id="me/TerraSentinel", reader=reader, policy=policy()
        )

        assert report["files"] == 4
        assert report["bytes"] == 1000
        assert report["megabytes"] == 0.0
        assert report["repo_id"] == "me/TerraSentinel"
        assert reader.calls[0]["revision"] == "main"

    def test_forwards_revision_and_include_patterns(self, tmp_path: Path) -> None:
        # Pinning a revision is what makes a run reproducible against a known lake
        # state, and what a model's dataset-commit lineage points at.
        reader = FakeReader()
        sync_bronze(
            local_dir=tmp_path / "lake",
            repo_id="me/TerraSentinel",
            revision="abc123",
            allow_patterns=["backfill/firms/**"],
            reader=reader,
            policy=policy(),
        )
        assert reader.calls[0]["revision"] == "abc123"
        assert reader.calls[0]["allow_patterns"] == ["backfill/firms/**"]

    def test_retries_transient_failures(self, tmp_path: Path) -> None:
        reader = FakeReader(fail_times=2)
        report = sync_bronze(
            local_dir=tmp_path / "lake", repo_id="me/TerraSentinel", reader=reader, policy=policy()
        )
        assert report["files"] == 3
        assert len(reader.calls) == 3

    def test_persistent_failure_raises(self, tmp_path: Path) -> None:
        reader = FakeReader(fail_times=99)
        with pytest.raises(TransientError):
            sync_bronze(
                local_dir=tmp_path / "lake", repo_id="me/TerraSentinel", reader=reader, policy=policy()
            )

    def test_resolves_the_configured_repo_when_unspecified(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HF_NAMESPACE", "swadhinbiswas")
        reader = FakeReader(files=1)
        report = sync_bronze(local_dir=tmp_path / "lake", reader=reader, policy=policy())
        assert report["repo_id"] == "swadhinbiswas/TerraSentinel"


class TestCli:
    def test_reports_a_failure_without_a_traceback(self, tmp_path: Path, capsys, monkeypatch) -> None:
        import tools.sync_bronze as module

        def boom(**_kwargs: object) -> None:
            raise TransientError("429")

        monkeypatch.setattr(module, "sync_bronze", boom)
        assert module.main(["--local-dir", str(tmp_path)]) == 2
        assert "could not mirror" in capsys.readouterr().err

    def test_json_output(self, tmp_path: Path, capsys, monkeypatch) -> None:
        import json

        import tools.sync_bronze as module

        def fake(**_kwargs: object) -> dict[str, object]:
            return {
                "repo_id": "me/x",
                "revision": "main",
                "local_dir": "d",
                "files": 1,
                "bytes": 10,
                "megabytes": 0.0,
            }

        monkeypatch.setattr(module, "sync_bronze", fake)
        assert module.main(["--local-dir", str(tmp_path), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["files"] == 1

    def test_main_is_importable_for_workflows(self) -> None:
        assert callable(main)
