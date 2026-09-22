"""Bronze manifest: which prefixes exist, and what globs dbt should receive.

This exists because both simpler approaches failed against real data: a two-glob
list dies when a prefix is missing (the post-backfill state), and a single leading
`**` works over hf:// but not on the local filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.bronze_manifest import (
    hub_manifest,
    local_manifest,
    main,
    manifest_from_paths,
    missing_sources,
)

ROOT = "hf://datasets/me/TerraSentinel"


class FakeReader:
    def __init__(self, files: list[str]) -> None:
        self.files = files
        self.calls = 0

    def list_files(self, prefix: str = "") -> list[str]:
        self.calls += 1
        return [path for path in self.files if path.startswith(prefix)]


class TestManifestFromPaths:
    def test_backfill_only_yields_only_the_backfill_glob(self) -> None:
        # The exact post-Phase-0 state that the old two-glob read could not survive.
        manifest = manifest_from_paths(
            ["backfill/firms/region=iberia_fire/year=2024/month=08/firms_x.parquet"],
            ROOT,
            source_ids=["firms"],
        )
        assert manifest == {"firms": [f"{ROOT}/backfill/firms/**/*.parquet"]}

    def test_live_only_yields_only_the_live_glob(self) -> None:
        manifest = manifest_from_paths(
            ["firms/region=iberia_fire/year=2026/month=09/day=21/firms_x.parquet"],
            ROOT,
            source_ids=["firms"],
        )
        assert manifest == {"firms": [f"{ROOT}/firms/**/*.parquet"]}

    def test_both_layouts_yield_both_globs_in_order(self) -> None:
        manifest = manifest_from_paths(
            [
                "firms/region=a/year=2026/month=09/day=21/x.parquet",
                "backfill/firms/region=a/year=2024/month=08/y.parquet",
            ],
            ROOT,
            source_ids=["firms"],
        )
        assert manifest["firms"] == [
            f"{ROOT}/firms/**/*.parquet",
            f"{ROOT}/backfill/firms/**/*.parquet",
        ]

    def test_absent_source_yields_no_globs(self) -> None:
        manifest = manifest_from_paths(["firms/a.parquet"], ROOT, source_ids=["firms", "sentinel"])
        assert manifest["sentinel"] == []

    def test_ops_files_do_not_count_as_source_data(self) -> None:
        # pipeline_runs manifests live in the same repo and must not be read as data.
        manifest = manifest_from_paths(
            ["ops/pipeline_runs/transform/run.json"], ROOT, source_ids=["firms"]
        )
        assert manifest["firms"] == []

    def test_similar_prefixes_are_not_confused(self) -> None:
        manifest = manifest_from_paths(
            ["firms_extra/a.parquet"], ROOT, source_ids=["firms"]
        )
        assert manifest["firms"] == []

    def test_every_glob_contains_a_single_globstar(self) -> None:
        # DuckDB's local glob rejects two '**' in one path; the manifest must never
        # emit such a pattern.
        manifest = manifest_from_paths(
            ["firms/a.parquet", "backfill/firms/b.parquet"],
            "/some/root",
            source_ids=["firms"],
        )
        for glob in manifest["firms"]:
            assert glob.count("**") == 1, glob

    def test_covers_every_configured_source_by_default(self) -> None:
        from collectors.config import SOURCES

        manifest = manifest_from_paths([], ROOT)
        assert set(manifest) == set(SOURCES)


class TestMissingSources:
    def test_reports_sources_without_globs(self) -> None:
        assert missing_sources({"firms": ["g"], "sentinel": []}) == ["sentinel"]

    def test_empty_when_all_present(self) -> None:
        assert missing_sources({"firms": ["g"]}) == []


class TestHubManifest:
    def test_lists_the_repo_once(self) -> None:
        reader = FakeReader(
            [
                "backfill/noaa_nsidc/region=arctic/year=2026/month=09/x.parquet",
                "ops/pipeline_runs/backfill_historical/run.json",
            ]
        )
        manifest = hub_manifest("me/TerraSentinel", reader=reader)
        assert reader.calls == 1
        assert manifest["noaa_nsidc"] == ["hf://datasets/me/TerraSentinel/backfill/noaa_nsidc/**/*.parquet"]
        assert manifest["firms"] == []


class TestLocalManifest:
    def test_walks_a_local_lake(self, tmp_path: Path) -> None:
        (tmp_path / "firms" / "region=a" / "year=2026").mkdir(parents=True)
        (tmp_path / "firms" / "region=a" / "year=2026" / "x.parquet").write_bytes(b"PAR1")
        (tmp_path / "backfill" / "firms" / "region=a").mkdir(parents=True)
        (tmp_path / "backfill" / "firms" / "region=a" / "y.parquet").write_bytes(b"PAR1")

        manifest = local_manifest(tmp_path, root="data/bronze")

        assert manifest["firms"] == [
            "data/bronze/firms/**/*.parquet",
            "data/bronze/backfill/firms/**/*.parquet",
        ]

    def test_missing_root_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            local_manifest(tmp_path / "nope")


class TestCli:
    def test_prints_dbt_vars(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "firms").mkdir()
        (tmp_path / "firms" / "x.parquet").write_bytes(b"PAR1")

        code = main(["--local-root", str(tmp_path), "--root", "root", "--allow-missing"])
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        assert payload["bronze_globs"]["firms"] == ["root/firms/**/*.parquet"]

    def test_exits_non_zero_when_a_source_is_absent(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "firms").mkdir()
        (tmp_path / "firms" / "x.parquet").write_bytes(b"PAR1")

        assert main(["--local-root", str(tmp_path), "--root", "root"]) == 1
        assert "run the backfill" in capsys.readouterr().err

    def test_writes_the_manifest_file_too(self, tmp_path: Path, capsys) -> None:
        lake = tmp_path / "lake"
        (lake / "firms").mkdir(parents=True)
        (lake / "firms" / "x.parquet").write_bytes(b"PAR1")
        destination = tmp_path / "out" / "globs.json"

        main(["--local-root", str(lake), "--root", "root", "--out", str(destination), "--allow-missing"])

        assert json.loads(destination.read_text())["firms"] == ["root/firms/**/*.parquet"]
        capsys.readouterr()

    def test_missing_local_root_exits_two(self, tmp_path: Path, capsys) -> None:
        assert main(["--local-root", str(tmp_path / "gone")]) == 2
        assert "could not build bronze manifest" in capsys.readouterr().err


class TestExclusionSelector:
    """An absent source must be excluded from the dbt build, not fail it.

    Without this, one un-backfilled source fails its staging model and dbt skips everything
    downstream — including the sync that publishes the marts which are ready. Measured on a
    real CI run: 94 models and tests passed and the Turso sync still never ran.
    """

    def test_absent_source_selects_its_whole_branch(self) -> None:
        from tools.bronze_manifest import exclusion_selector

        selector = exclusion_selector({"firms": ["g"], "sentinel": [], "noaa_nsidc": []})
        assert "stg_sentinel+" in selector
        assert "stg_noaa+" in selector
        assert "stg_firms" not in selector

    def test_all_present_yields_an_empty_selector(self) -> None:
        from tools.bronze_manifest import exclusion_selector

        assert exclusion_selector({"firms": ["g"], "sentinel": ["g"], "noaa_nsidc": ["g"]}) == ""

    def test_selector_is_written_to_a_file(self, tmp_path: Path, capsys) -> None:
        lake = tmp_path / "lake"
        (lake / "firms").mkdir(parents=True)
        (lake / "firms" / "x.parquet").write_bytes(b"PAR1")
        destination = tmp_path / "exclude.txt"

        code = main([
            "--local-root", str(lake), "--root", "root",
            "--emit-exclude", str(destination), "--allow-missing",
        ])

        assert code == 0
        assert "stg_sentinel+" in destination.read_text()
        capsys.readouterr()
