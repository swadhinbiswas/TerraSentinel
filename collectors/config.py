"""Central configuration for the collection layer.

Everything that would otherwise be a magic constant lives here: study regions,
source registry, H3 resolutions, bronze path layout, and credential access.

Credentials are read from the environment only (`.env` locally, GitHub
Encrypted Secrets in CI, Cloudflare Pages env vars for the dashboard). Nothing
in this module ever logs or returns a secret value for display.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
BRONZE_STAGING_DIR = DATA_DIR / "bronze"
SILVER_STAGING_DIR = DATA_DIR / "silver"

load_dotenv(REPO_ROOT / ".env")

AnomalyType = Literal["fire", "deforestation", "ice"]
BBox = tuple[float, float, float, float]
"""Bounding box as (west, south, east, north) in EPSG:4326 degrees."""


class MissingCredential(RuntimeError):
    """A required credential was absent from the environment.

    Carries the full list of missing names so a workflow fails once with a
    complete message instead of one variable at a time.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        bullets = "\n".join(f"  - {name}" for name in self.missing)
        super().__init__(
            f"{len(self.missing)} required environment variable(s) are not set:\n{bullets}\n"
            "Set them in .env locally (see .env.example) or as GitHub Encrypted Secrets in CI."
        )


def get_secret(name: str, *, required: bool = True) -> str | None:
    """Read a credential from the environment without ever leaking its value."""
    value = os.environ.get(name)
    if value is not None:
        value = value.strip()
    if not value:
        if required:
            raise MissingCredential([name])
        return None
    return value


def require_secrets(names: Sequence[str]) -> dict[str, str]:
    """Read several credentials at once, reporting every missing name together."""
    missing = [name for name in names if not (os.environ.get(name) or "").strip()]
    if missing:
        raise MissingCredential(missing)
    return {name: get_secret(name) for name in names}  # type: ignore[misc]


def secret_status(names: Sequence[str]) -> dict[str, str]:
    """Return ``{"HF_TOKEN": "set", ...}`` — safe to log and store."""
    return {name: ("set" if (os.environ.get(name) or "").strip() else "missing") for name in names}


# --------------------------------------------------------------------------
# Hugging Face Hub repositories
# --------------------------------------------------------------------------

#: Project name. A module constant, not an env var: it is not a deployment
#: parameter, and one fewer thing to configure is one fewer thing to get wrong.
PROJECT = "TerraSentinel"

#: Repo-name suffix per logical store. ``bronze`` sits at the repo root, so the
#: data repo reads as ``<namespace>/TerraSentinel`` with lake paths inside it.
_REPO_SUFFIX: dict[str, str] = {
    "bronze": "",
    "silver": "-silver",
    "models": "-models",
}

_REPO_OVERRIDE: dict[str, str] = {
    "bronze": "HF_BRONZE_REPO",
    "silver": "HF_SILVER_REPO",
    "models": "HF_MODEL_REPO",
}


def hf_repo(kind: Literal["bronze", "silver", "models"]) -> str:
    """Resolve a Hub repo id, e.g. ``swadhinbiswas/TerraSentinel``.

    Repo ids on the Hub are always ``owner/name`` and the token does not imply the
    owner — a token authenticates a user who may push to several orgs — so the
    namespace has to be explicit. The three stores then derive from it::

        <namespace>/TerraSentinel           bronze lake   (dataset repo)
        <namespace>/TerraSentinel-silver    silver        (dataset repo)
        <namespace>/TerraSentinel-models    model registry (model repo)

    A model registry cannot share a dataset repo: Hub repo *type* is part of the
    address, and model cards and revisions are model-repo features.

    Any store can instead be pointed somewhere arbitrary with its override var.
    """
    explicit = (os.environ.get(_REPO_OVERRIDE[kind]) or "").strip()
    if explicit:
        return explicit
    namespace = get_secret("HF_NAMESPACE")
    return f"{namespace}/{PROJECT}{_REPO_SUFFIX[kind]}"


# --------------------------------------------------------------------------
# Study regions
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Region:
    """A named area of interest that observations are attributed to.

    Observations carry an H3 index (real spatial bucketing) *and* a region id
    (human-meaningful grouping for dashboards, models and validation).
    """

    region_id: str
    name: str
    anomaly_type: AnomalyType
    bbox: BBox
    h3_resolution: int = 7
    description: str = ""

    def __post_init__(self) -> None:
        west, south, east, north = self.bbox
        if not (-180.0 <= west < east <= 180.0):
            raise ValueError(f"{self.region_id}: invalid longitude span {west}..{east}")
        if not (-90.0 <= south < north <= 90.0):
            raise ValueError(f"{self.region_id}: invalid latitude span {south}..{north}")
        if not (0 <= self.h3_resolution <= 15):
            raise ValueError(f"{self.region_id}: h3_resolution {self.h3_resolution} out of range")

    @property
    def area_slug(self) -> str:
        return self.region_id

    def contains(self, lat: float, lon: float) -> bool:
        west, south, east, north = self.bbox
        return west <= lon <= east and south <= lat <= north

    def bbox_csv(self) -> str:
        """FIRMS area-API coordinate format: ``west,south,east,north``."""
        west, south, east, north = self.bbox
        return f"{west},{south},{east},{north}"

    def approx_area_km2(self) -> float:
        """Rough planar approximation — used for logging request volume only."""
        import math

        west, south, east, north = self.bbox
        mean_lat = math.radians((south + north) / 2.0)
        height = (north - south) * 110.574
        width = (east - west) * 111.320 * math.cos(mean_lat)
        return abs(height * width)


#: Three EU study zones, one per anomaly type, plus hemispheric ice baselines.
#: H3 resolution 7 (~5.2 km^2 cells) for point sources; 5 (~252 km^2) for the
#: coarse 0.25-degree NOAA grids and hemisphere-scale sea-ice series.
REGIONS: tuple[Region, ...] = (
    Region(
        region_id="iberia_fire",
        name="Iberian Peninsula",
        anomaly_type="fire",
        bbox=(-10.0, 35.5, 3.5, 43.9),
        h3_resolution=7,
        description="Spain + Portugal; Mediterranean fire regime, strong FIRMS archive.",
    ),
    Region(
        region_id="greece_fire",
        name="Greece & Aegean",
        anomaly_type="fire",
        bbox=(19.0, 34.4, 28.5, 41.8),
        h3_resolution=7,
        description="Attica/Euboea/Anatolia-facing fire corridor with recurring megafires.",
    ),
    Region(
        region_id="carpathian_deforest",
        name="Romanian Carpathians",
        anomaly_type="deforestation",
        bbox=(21.0, 43.9, 27.6, 48.3),
        h3_resolution=7,
        description="EU's highest-rate illegal logging frontier; Sentinel-2 NDVI monitoring.",
    ),
    Region(
        region_id="alps_ice",
        name="European Alps",
        anomaly_type="ice",
        bbox=(5.4, 43.7, 16.6, 48.3),
        h3_resolution=7,
        description="Alpine glacier mass balance via Sentinel-1 SAR backscatter.",
    ),
    Region(
        region_id="norway_ice",
        name="Norway & Svalbard glaciers",
        anomaly_type="ice",
        bbox=(4.0, 58.0, 31.0, 80.5),
        h3_resolution=7,
        description="Scandinavian + Svalbard ice caps; SAR unaffected by polar night.",
    ),
    Region(
        region_id="arctic",
        name="Arctic sea ice",
        anomaly_type="ice",
        bbox=(-180.0, 60.0, 180.0, 90.0),
        h3_resolution=5,
        description="Hemispheric baseline for the NSIDC Sea Ice Index extent series.",
    ),
    Region(
        region_id="antarctic",
        name="Antarctic sea ice",
        anomaly_type="ice",
        bbox=(-180.0, -90.0, 180.0, -55.0),
        h3_resolution=5,
        description="Southern-hemisphere control series for the ice model.",
    ),
)

REGION_INDEX: dict[str, Region] = {region.region_id: region for region in REGIONS}


def regions_for_anomaly_type(anomaly_type: AnomalyType) -> tuple[Region, ...]:
    return tuple(region for region in REGIONS if region.anomaly_type == anomaly_type)


def get_region(region_id: str) -> Region:
    try:
        return REGION_INDEX[region_id]
    except KeyError as exc:
        raise KeyError(
            f"Unknown region_id {region_id!r}. Known: {sorted(REGION_INDEX)}"
        ) from exc


# --------------------------------------------------------------------------
# Source registry
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Everything the pipeline needs to know about one upstream data source."""

    source_id: str
    label: str
    metric_types: tuple[str, ...]
    anomaly_types: tuple[AnomalyType, ...]
    h3_resolution: int
    cadence_cron: str
    cadence_human: str
    live_products: tuple[str, ...]
    backfill_products: tuple[str, ...]
    required_secrets: tuple[str, ...]
    attribution: str
    docs_url: str = ""
    notes: str = ""
    #: ``(standard-processing, near-real-time)`` pairs for one instrument.
    #:
    #: Both archives are needed and neither is sufficient alone: SP is published
    #: roughly three months behind real time, and NRT is *retained* for roughly
    #: three months. Backfilling with SP alone therefore returns silently empty
    #: results for the most recent quarter — measured against the live API, not
    #: assumed (see docs/adr/0008-firms-product-selection.md).
    product_pairs: tuple[tuple[str, str], ...] = ()

    def as_metadata(self) -> dict[str, object]:
        """Serialisable subset for pipeline_runs and the dashboard."""
        return {
            "source_id": self.source_id,
            "label": self.label,
            "metric_types": list(self.metric_types),
            "anomaly_types": list(self.anomaly_types),
            "h3_resolution": self.h3_resolution,
            "cadence_cron": self.cadence_cron,
            "attribution": self.attribution,
        }


SOURCES: dict[str, SourceSpec] = {
    "firms": SourceSpec(
        source_id="firms",
        label="NASA FIRMS active fire detections",
        metric_types=("fire_radiative_power", "brightness"),
        anomaly_types=("fire",),
        h3_resolution=7,
        cadence_cron="15 */6 * * *",
        cadence_human="every 6 hours",
        live_products=("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT"),
        backfill_products=("VIIRS_SNPP_SP", "MODIS_SP", "VIIRS_NOAA20_SP"),
        product_pairs=(
            ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
            ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT"),
            ("MODIS_SP", "MODIS_NRT"),
        ),
        required_secrets=("FIRMS_MAP_KEY",),
        attribution="Fire data: NASA FIRMS (MODIS and VIIRS active fire products).",
        docs_url="https://firms.modaps.eosdis.nasa.gov/api/area/",
        notes=(
            "Area API allows at most 5 days per request, so backfills iterate in "
            "5-day windows. Product choice is driven by window age, not by whether "
            "the run is a backfill: SP and NRT cover different periods and the "
            "collector falls back between them."
        ),
    ),
    "sentinel": SourceSpec(
        source_id="sentinel",
        label="Copernicus Sentinel-2 NDVI / Sentinel-1 SAR via Google Earth Engine",
        metric_types=("ndvi", "sar_backscatter"),
        anomaly_types=("deforestation", "ice"),
        h3_resolution=7,
        cadence_cron="30 3 * * *",
        cadence_human="daily",
        live_products=("COPERNICUS/S2_SR_HARMONIZED", "COPERNICUS/S1_GRD"),
        backfill_products=("COPERNICUS/S2_SR_HARMONIZED", "COPERNICUS/S1_GRD"),
        required_secrets=(
            "GEE_SERVICE_ACCOUNT_JSON",
            "GEE_SERVICE_ACCOUNT_EMAIL",
            "GEE_PROJECT",
        ),
        attribution="Imagery: Copernicus Sentinel-2 and Sentinel-1 (ESA), processed in Google Earth Engine.",
        docs_url="https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED",
        notes=(
            "Only extracted numeric composites are stored — never full-resolution "
            "scenes (HF storage is for features, not imagery)."
        ),
    ),
    "noaa_nsidc": SourceSpec(
        source_id="noaa_nsidc",
        label="NOAA OISST sea-surface temperature anomaly + NSIDC Sea Ice Index",
        metric_types=("sst_anomaly", "sea_ice_extent", "sea_ice_extent_climatology"),
        anomaly_types=("fire", "ice"),
        h3_resolution=5,
        cadence_cron="45 4 * * 1",
        cadence_human="weekly",
        live_products=("noaa_oisst_v2_1_anom", "nsidc_seaice_index_v4_0"),
        backfill_products=("noaa_oisst_v2_1_anom", "nsidc_seaice_index_v4_0"),
        required_secrets=(),
        attribution="Ocean and ice data: NOAA OISST v2.1 and NSIDC Sea Ice Index (v4.0).",
        docs_url="https://nsidc.org/data/g02135",
        notes=(
            "Three arms: daily gridded SST anomaly from NOAA PSL over OPeNDAP, "
            "hemispheric sea-ice extent from the NSIDC daily series, and the "
            "static 1981-2010 NSIDC climatology used as the seasonal baseline. "
            "Both archives are keyless public HTTPS; weekly cadence is sufficient."
        ),
    ),
}


def resolve_source_registry() -> dict[str, dict[str, Any]]:
    """The source registry as plain data: metadata plus credential *presence*.

    Safe to log, serialise or serve — values are never included.
    """
    return {
        spec.source_id: {
            **spec.as_metadata(),
            "required_secrets": list(spec.required_secrets),
            "credentials": secret_status(spec.required_secrets),
            "docs_url": spec.docs_url,
            "notes": spec.notes,
        }
        for spec in SOURCES.values()
    }


def preflight_sources(source_ids: Sequence[str]) -> list[dict[str, str]]:
    """Verify every secret required by the given sources, failing once with all gaps.

    Returns a per-source credential status list (``set``/``missing`` only),
    which is safe to write into pipeline_runs.
    """
    required: list[str] = []
    for source_id in source_ids:
        try:
            spec = SOURCES[source_id]
        except KeyError as exc:
            raise KeyError(
                f"Unknown source {source_id!r}. Known: {sorted(SOURCES)}"
            ) from exc
        required.extend(spec.required_secrets)
    require_secrets(sorted(set(required)))
    return [
        {"source_id": source_id, **secret_status(SOURCES[source_id].required_secrets)}
        for source_id in source_ids
    ]


# --------------------------------------------------------------------------
# Bronze path layout (HF Hub dataset repo, hive-partitioned)
# --------------------------------------------------------------------------

def run_fingerprint(parts: Sequence[object], *, length: int = 12) -> str:
    """Stable short hash of the inputs a run was built from.

    Used to make HF paths idempotent: the same logical extract lands on the
    same path, so a re-run overwrites rather than duplicating rows.
    """
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8"))
    return digest.hexdigest()[:length]


def bronze_prefix(
    source_id: str,
    *,
    region_id: str,
    year: int,
    month: int,
    day: int | None = None,
    backfill: bool = False,
) -> str:
    """Hive-partitioned bronze prefix.

    Live:     ``firms/region=iberia_fire/year=2026/month=09/day=21``
    Backfill: ``backfill/firms/region=iberia_fire/year=2026/month=09``
    """
    if source_id not in SOURCES:
        raise KeyError(f"Unknown source {source_id!r}. Known: {sorted(SOURCES)}")
    if not 1 <= month <= 12:
        raise ValueError(f"month {month} out of range")
    if day is not None and not 1 <= day <= 31:
        raise ValueError(f"day {day} out of range")
    if backfill and day is not None:
        raise ValueError("backfill partitions are monthly; omit day")

    parts = ["backfill"] if backfill else []
    parts += [source_id, f"region={region_id}", f"year={year:04d}", f"month={month:02d}"]
    if day is not None:
        parts.append(f"day={day:02d}")
    return "/".join(parts)


def bronze_filename(
    source_id: str,
    region_id: str,
    *,
    year: int,
    month: int,
    day: int | None = None,
    backfill: bool = False,
) -> str:
    if backfill:
        stamp = f"{year:04d}{month:02d}"
    else:
        if day is None:
            raise ValueError("live partitions require a day")
        stamp = f"{year:04d}{month:02d}{day:02d}"
    return f"{source_id}_{region_id}_{stamp}.parquet"


def parquet_uri(repo_id: str, path_in_repo: str, *, revision: str = "main") -> str:
    """Resolve URL DuckDB can read directly — no intermediate download step."""
    return f"https://huggingface.co/datasets/{repo_id}/resolve/{revision}/{path_in_repo}"


#: Frost/quality guard rails shared by collectors and tests.
FIRMS_MIN_CONFIDENCE_MODES: tuple[str, ...] = ("l", "n", "h")
DEFAULT_USER_AGENT = "TerraSentinel/0.1 (+https://github.com/; contact: pipeline operator)"
