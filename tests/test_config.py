"""Hub repo resolution: one namespace plus one project name yields three repos.

Getting this wrong is a quiet, expensive failure — a collector happily writes a
lake into the wrong account, or a model lands in a dataset repo where the registry
conventions do not apply.
"""

from __future__ import annotations

import re

import pytest

from collectors.config import (
    MissingCredential,
    get_region,
    hf_repo,
    regions_for_anomaly_type,
    resolve_source_registry,
)

HUB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HF_NAMESPACE",
        "HF_BRONZE_REPO",
        "HF_SILVER_REPO",
        "HF_MODEL_REPO",
    ):
        monkeypatch.delenv(name, raising=False)


class TestRepoDerivation:
    def test_bronze_is_namespace_slash_project(self, monkeypatch) -> None:
        # The project name is a code constant, not configuration.
        monkeypatch.setenv("HF_NAMESPACE", "swadhinbiswas")
        assert hf_repo("bronze") == "swadhinbiswas/TerraSentinel"

    def test_silver_and_models_are_suffixed(self, monkeypatch) -> None:
        monkeypatch.setenv("HF_NAMESPACE", "swadhinbiswas")
        assert hf_repo("silver") == "swadhinbiswas/TerraSentinel-silver"
        assert hf_repo("models") == "swadhinbiswas/TerraSentinel-models"

    def test_models_are_a_distinct_repo_from_the_lake(self, monkeypatch) -> None:
        # Hub repo *type* is part of the address, so a model registry cannot live
        # in the dataset repo.
        monkeypatch.setenv("HF_NAMESPACE", "swadhinbiswas")
        assert hf_repo("models") != hf_repo("bronze")

    def test_organisation_namespace_works_the_same(self, monkeypatch) -> None:
        monkeypatch.setenv("HF_NAMESPACE", "nanoLMs")
        assert hf_repo("bronze") == "nanoLMs/TerraSentinel"
        assert hf_repo("models") == "nanoLMs/TerraSentinel-models"

    @pytest.mark.parametrize("kind", ["bronze", "silver", "models"])
    def test_explicit_override_wins(self, monkeypatch, kind: str) -> None:
        monkeypatch.setenv("HF_NAMESPACE", "ignored")
        monkeypatch.setenv(
            {"bronze": "HF_BRONZE_REPO", "silver": "HF_SILVER_REPO", "models": "HF_MODEL_REPO"}[kind],
            "somewhere/special",
        )
        assert hf_repo(kind) == "somewhere/special"

    def test_override_does_not_need_a_namespace(self, monkeypatch) -> None:
        monkeypatch.setenv("HF_BRONZE_REPO", "somewhere/special")
        assert hf_repo("bronze") == "somewhere/special"

    def test_missing_namespace_fails_loudly(self) -> None:
        with pytest.raises(MissingCredential, match="HF_NAMESPACE"):
            hf_repo("bronze")

    def test_namespace_is_whitespace_tolerant(self, monkeypatch) -> None:
        monkeypatch.setenv("HF_NAMESPACE", " swadhinbiswas ")
        assert hf_repo("bronze") == "swadhinbiswas/TerraSentinel"

    @pytest.mark.parametrize("kind", ["bronze", "silver", "models"])
    def test_derived_ids_are_valid_hub_names(self, monkeypatch, kind: str) -> None:
        monkeypatch.setenv("HF_NAMESPACE", "swadhinbiswas")
        owner, _, name = hf_repo(kind).partition("/")
        assert HUB_NAME.match(owner), owner
        assert HUB_NAME.match(name), name


class TestRegionLookups:
    def test_fire_regions(self) -> None:
        ids = {region.region_id for region in regions_for_anomaly_type("fire")}
        assert ids == {"iberia_fire", "greece_fire"}

    def test_unknown_region_names_the_known_ones(self) -> None:
        with pytest.raises(KeyError, match="Known:"):
            get_region("atlantis")


class TestSourceRegistry:
    def test_every_source_declares_its_secrets(self) -> None:
        registry = resolve_source_registry()
        assert registry["noaa_nsidc"]["required_secrets"] == []
        assert "FIRMS_MAP_KEY" in registry["firms"]["required_secrets"]
        assert "GEE_PROJECT" in registry["sentinel"]["required_secrets"]

    def test_registry_reports_credential_presence_not_values(self, monkeypatch) -> None:
        monkeypatch.setenv("FIRMS_MAP_KEY", "a-secret-value-should-not-appear")
        registry = resolve_source_registry()
        assert registry["firms"]["credentials"] == {"FIRMS_MAP_KEY": "set"}
        assert "a-secret-value-should-not-appear" not in repr(registry)
