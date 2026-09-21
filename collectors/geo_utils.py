"""Geospatial helpers shared by every collector.

Point observations are indexed into H3 hexagons at ingestion time. H3 bucketing
is what makes downstream work tractable: regional aggregation, "what else is
near this anomaly" queries, and map rendering all become cheap grouped reads on
a single string column instead of spatial joins on raw coordinates.

Pure functions only — no I/O, no network — so this module is trivially testable
and safe to import anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import h3
import pandas as pd

from collectors.config import REGIONS, BBox, Region

__all__ = [
    "add_h3_index",
    "bbox_contains",
    "bbox_geojson_polygon",
    "cell_boundary",
    "cell_center",
    "cell_k_ring",
    "cell_parent",
    "cells_for_bbox",
    "h3_cell",
    "is_valid_cell",
    "region_for_point",
    "validate_latlon",
]


def validate_latlon(lat: float, lon: float) -> None:
    """Fail loudly on out-of-range or non-finite coordinates."""
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
        raise ValueError(f"non-finite coordinate: lat={lat!r} lon={lon!r}")
    if not -90.0 <= float(lat) <= 90.0:
        raise ValueError(f"latitude {lat!r} outside [-90, 90]")
    if not -180.0 <= float(lon) <= 180.0:
        raise ValueError(f"longitude {lon!r} outside [-180, 180]")


def h3_cell(lat: float, lon: float, resolution: int) -> str:
    """H3 cell id for a point (H3 v4 API: ``latlng_to_cell``)."""
    validate_latlon(lat, lon)
    return h3.latlng_to_cell(float(lat), float(lon), resolution)


def is_valid_cell(cell: str) -> bool:
    return h3.is_valid_cell(cell)


def cell_center(cell: str) -> tuple[float, float]:
    """``(lat, lon)`` centre of a cell — used to plot hex aggregates on a map."""
    return h3.cell_to_latlng(cell)


def cell_boundary(cell: str) -> list[tuple[float, float]]:
    """Closed ring of ``(lat, lon)`` vertices, for rendering the hex on a map."""
    ring = list(h3.cell_to_boundary(cell))
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def cell_parent(cell: str, resolution: int) -> str:
    """Roll a cell up to a coarser resolution (H3 resolutions are hierarchical)."""
    return h3.cell_to_parent(cell, resolution)


def cell_k_ring(cell: str, k: int) -> list[str]:
    """The cell plus its ``k``-ring neighbours — powers 'anomalies near here'."""
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")
    return list(h3.grid_disk(cell, k))


def bbox_contains(bbox: BBox, lat: float, lon: float) -> bool:
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


def bbox_geojson_polygon(bbox: BBox) -> dict[str, Any]:
    """Closed GeoJSON polygon in ``(lon, lat)`` order, as h3/EE expect."""
    west, south, east, north = bbox
    if not (south < north):
        raise ValueError(f"bbox has zero/negative height: {bbox}")
    ring = [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def cells_for_bbox(
    bbox: BBox,
    resolution: int,
    *,
    max_cells: int = 500_000,
) -> list[str]:
    """All H3 cells covering a bbox, used to build a dense grid spine.

    A dense spine matters for aggregation: without it, a region/time window with
    zero observations simply vanishes from a ``GROUP BY``, which looks identical
    to "no anomaly" in the gold layer.
    """
    cells = set(h3.geo_to_cells(bbox_geojson_polygon(bbox), resolution))
    if len(cells) > max_cells:
        raise ValueError(
            f"bbox {bbox} at resolution {resolution} yields {len(cells)} cells "
            f"(> max_cells={max_cells}); coarsen the resolution or shrink the bbox"
        )
    return sorted(cells)


def region_for_point(
    lat: float,
    lon: float,
    regions: Sequence[Region] = REGIONS,
) -> str | None:
    """Region id containing a point, or ``None``.

    Regions are checked smallest-area-first so that overlapping study zones
    resolve to the most specific one deterministically.
    """
    validate_latlon(lat, lon)
    ordered = sorted(regions, key=lambda region: region.approx_area_km2())
    for region in ordered:
        if region.contains(lat, lon):
            return region.region_id
    return None


def add_h3_index(
    frame: pd.DataFrame,
    resolution: int,
    *,
    lat_col: str = "latitude",
    lon_col: str = "longitude",
    out_col: str = "h3_index",
    region_col: str = "region_id",
    regions: Sequence[Region] = REGIONS,
    assign_regions: bool = True,
) -> pd.DataFrame:
    """Attach ``h3_index`` (and optionally ``region_id``) to every row.

    Rows with unusable coordinates are dropped rather than silently coerced to
    a cell on the null island — the caller gets the exact count removed via a
    logged warning, and the returned frame is a copy so input is never mutated.
    """
    if lat_col not in frame.columns or lon_col not in frame.columns:
        raise KeyError(f"expected coordinate columns {lat_col!r}/{lon_col!r} in {list(frame.columns)}")
    if resolution < 0 or resolution > 15:
        raise ValueError(f"resolution {resolution} out of range [0, 15]")

    out = frame.copy()
    lats = pd.to_numeric(out[lat_col], errors="coerce")
    lons = pd.to_numeric(out[lon_col], errors="coerce")
    usable = lats.between(-90.0, 90.0) & lons.between(-180.0, 180.0)

    dropped = int((~usable).sum())
    if dropped:
        import logging

        logging.getLogger(__name__).warning(
            "add_h3_index: dropping %d row(s) with invalid coordinates", dropped
        )

    out = out.loc[usable].copy()
    out[lat_col] = lats.loc[usable].astype("float64")
    out[lon_col] = lons.loc[usable].astype("float64")

    # A collector that already knows its spatial grain (GEE zonal statistics are
    # computed against a fixed H3 grid, not per row) keeps its own indices; only
    # genuinely missing ones are derived here.
    if out_col in out.columns:
        existing = out[out_col]
        needs_index = existing.isna() | (existing.astype("string").fillna("").str.strip() == "")
    else:
        out[out_col] = pd.Series(None, index=out.index, dtype="object")
        needs_index = pd.Series(True, index=out.index)

    if needs_index.any():
        subset = out.loc[needs_index]
        out.loc[needs_index, out_col] = [
            h3.latlng_to_cell(lat, lon, resolution)
            for lat, lon in zip(
                subset[lat_col].to_numpy(), subset[lon_col].to_numpy(), strict=True
            )
        ]

    if assign_regions and region_col not in out.columns:
        ordered = sorted(regions, key=lambda region: region.approx_area_km2())
        assignment = pd.Series(None, index=out.index, dtype="object")
        for region in ordered:
            west, south, east, north = region.bbox
            mask = (
                out[lon_col].between(west, east)
                & out[lat_col].between(south, north)
                & assignment.isna()
            )
            assignment.loc[mask] = region.region_id
        out[region_col] = assignment

    return out


def cells_to_region_map(
    cells: Iterable[str],
    regions: Sequence[Region] = REGIONS,
) -> dict[str, str | None]:
    """Map each H3 cell to the most specific region containing its centre."""
    return {cell: region_for_point(*cell_center(cell), regions=regions) for cell in cells}
