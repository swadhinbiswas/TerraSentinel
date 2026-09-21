"""Geospatial helpers: H3 indexing, region attribution, grid spines."""

from __future__ import annotations

import h3
import pandas as pd
import pytest

from collectors.config import REGIONS, get_region
from collectors.geo_utils import (
    add_h3_index,
    bbox_contains,
    bbox_geojson_polygon,
    cell_boundary,
    cell_center,
    cell_k_ring,
    cell_parent,
    cells_for_bbox,
    h3_cell,
    is_valid_cell,
    region_for_point,
    validate_latlon,
)


class TestValidateLatLon:
    def test_accepts_valid_point(self) -> None:
        validate_latlon(45.0, -8.0)

    @pytest.mark.parametrize(
        "lat,lon",
        [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0), (float("nan"), 0.0)],
    )
    def test_rejects_out_of_range(self, lat: float, lon: float) -> None:
        with pytest.raises(ValueError):
            validate_latlon(lat, lon)


class TestH3Cell:
    def test_derives_expected_cell(self) -> None:
        cell = h3_cell(39.4521, -8.1234, 7)
        assert is_valid_cell(cell)
        assert h3.get_resolution(cell) == 7

    def test_cell_is_stable_for_same_point(self) -> None:
        assert h3_cell(39.45, -8.12, 7) == h3_cell(39.45, -8.12, 7)

    def test_coarser_resolution_matches_parent(self) -> None:
        fine = h3_cell(39.4521, -8.1234, 8)
        coarse = h3_cell(39.4521, -8.1234, 6)
        assert cell_parent(fine, 6) == coarse

    def test_center_round_trips(self) -> None:
        cell = h3_cell(39.4521, -8.1234, 7)
        lat, lon = cell_center(cell)
        assert h3_cell(lat, lon, 7) == cell

    def test_boundary_is_closed_ring(self) -> None:
        ring = cell_boundary(h3_cell(39.45, -8.12, 7))
        assert len(ring) == 7
        assert ring[0] == ring[-1]

    def test_k_ring_size_follows_hex_formula(self) -> None:
        cell = h3_cell(39.45, -8.12, 7)
        assert len(cell_k_ring(cell, 0)) == 1
        assert len(cell_k_ring(cell, 2)) == 1 + 3 * 2 * 3

    def test_negative_k_ring_rejected(self) -> None:
        with pytest.raises(ValueError):
            cell_k_ring(h3_cell(39.45, -8.12, 7), -1)


class TestRegionForPoint:
    def test_assigns_iberia(self) -> None:
        assert region_for_point(39.45, -8.12) == "iberia_fire"

    def test_assigns_carpathians(self) -> None:
        assert region_for_point(45.6, 24.5) == "carpathian_deforest"

    def test_returns_none_outside_every_region(self) -> None:
        assert region_for_point(-35.0, 140.0) is None

    def test_prefers_most_specific_region_when_overlapping(self) -> None:
        # The Arctic bbox spans everything north of 60N, including Norway's study
        # zone; the smaller zone must win so labels stay meaningful.
        assert region_for_point(61.0, 7.0) == "norway_ice"
        assert region_for_point(78.0, 15.0) == "norway_ice"
        assert region_for_point(85.0, -40.0) == "arctic"


class TestAddH3Index:
    def test_adds_index_and_region(self) -> None:
        frame = pd.DataFrame({"latitude": [39.45, 45.6], "longitude": [-8.12, 24.5]})
        out = add_h3_index(frame, 7)
        assert "h3_index" in out.columns
        assert list(out["region_id"]) == ["iberia_fire", "carpathian_deforest"]
        assert all(is_valid_cell(cell) for cell in out["h3_index"])

    def test_drops_invalid_coordinates(self) -> None:
        frame = pd.DataFrame(
            {
                "latitude": [39.45, 999.0, None],
                "longitude": [-8.12, 0.0, 0.0],
            }
        )
        out = add_h3_index(frame, 7)
        assert len(out) == 1

    def test_does_not_mutate_input(self) -> None:
        frame = pd.DataFrame({"latitude": [39.45], "longitude": [-8.12]})
        add_h3_index(frame, 7)
        assert "h3_index" not in frame.columns

    def test_preserves_collector_supplied_cells(self) -> None:
        # GEE computes statistics against a fixed H3 grid; those indices must survive.
        preset = h3_cell(45.0, 24.0, 5)
        frame = pd.DataFrame(
            {
                "latitude": [45.0, 45.5],
                "longitude": [24.0, 24.5],
                "h3_index": [preset, None],
            }
        )
        out = add_h3_index(frame, 7)
        assert out["h3_index"].iloc[0] == preset
        assert h3.get_resolution(out["h3_index"].iloc[1]) == 7

    def test_accepts_explicit_region_and_leaves_it_alone(self) -> None:
        frame = pd.DataFrame(
            {
                "latitude": [10.0],
                "longitude": [10.0],
                "region_id": ["arctic"],
                "h3_index": [h3_cell(10.0, 10.0, 5)],
            }
        )
        out = add_h3_index(frame, 5)
        assert out["region_id"].iloc[0] == "arctic"

    def test_missing_coordinate_column_raises(self) -> None:
        with pytest.raises(KeyError):
            add_h3_index(pd.DataFrame({"lat": [1.0]}), 7)

    def test_invalid_resolution_raises(self) -> None:
        frame = pd.DataFrame({"latitude": [1.0], "longitude": [1.0]})
        with pytest.raises(ValueError):
            add_h3_index(frame, 99)


class TestCellsForBbox:
    def test_returns_sorted_unique_cells(self) -> None:
        cells = cells_for_bbox(get_region("carpathian_deforest").bbox, 5)
        assert len(cells) == len(set(cells))
        assert cells == sorted(cells)
        assert 900 < len(cells) < 1000

    def test_raises_when_too_many_cells(self) -> None:
        with pytest.raises(ValueError, match="max_cells"):
            cells_for_bbox(get_region("norway_ice").bbox, 7, max_cells=1000)

    def test_rejects_degenerate_bbox(self) -> None:
        with pytest.raises(ValueError):
            bbox_geojson_polygon((0.0, 10.0, 10.0, 10.0))


class TestBboxHelpers:
    def test_contains(self) -> None:
        bbox = (-10.0, 35.5, 3.5, 43.9)
        assert bbox_contains(bbox, 39.0, -8.0)
        assert not bbox_contains(bbox, 39.0, -20.0)

    def test_geojson_polygon_is_closed_and_lon_lat(self) -> None:
        polygon = bbox_geojson_polygon((-10.0, 35.5, 3.5, 43.9))
        ring = polygon["coordinates"][0]
        assert polygon["type"] == "Polygon"
        assert ring[0] == ring[-1]
        assert ring[0] == [-10.0, 35.5]


def test_every_region_round_trips_through_h3() -> None:
    for region in REGIONS:
        lat = (region.bbox[1] + region.bbox[3]) / 2.0
        lon = (region.bbox[0] + region.bbox[2]) / 2.0
        assert is_valid_cell(h3_cell(lat, lon, region.h3_resolution))
