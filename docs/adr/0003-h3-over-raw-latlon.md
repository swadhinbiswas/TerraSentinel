# ADR 0003 — H3 hexagons over raw lat/lon for spatial indexing

**Status:** accepted · implemented in `collectors/geo_utils.py`

## Context

Every observation arrives as a point (or a grid cell) with a latitude and longitude. The
pipeline needs to aggregate by region, ask "what else is near this anomaly", render points on
a map, and compare the same place across time. Raw coordinates make all four awkward: grouping
on floats produces near-duplicate buckets, and proximity queries become distance maths.

## Decision

Every observation is assigned an **H3 cell id at ingestion** (resolution 7, ~5.2 km², for point
sources; resolution 5, ~253 km², for coarse grids), stored as a string column alongside the
original coordinates. Region ids are assigned by bounding box, smallest region first, so
overlapping study zones resolve deterministically.

## Why

- **Grouping becomes a string equality.** A fire count per cell per day is a `GROUP BY`, not a
  spatial join. No distance functions, no projected coordinate systems, no epsilon.
- **Hierarchy is free.** H3 resolutions nest, so res-7 cells roll up into res-6 and res-5
  parents without reprojecting anything. One ingestion decision serves every later grain.
- **It is the industry standard for point bucketing** — the same reason Uber built it. Using
  the standard thing is itself a design argument.
- **Proximity is cheap.** `grid_disk(cell, k)` gives the neighbours for "show me anomalies
  near this one" without a spatial index or a DB extension.
- **Coordinates are kept anyway.** H3 is an index, not a replacement: the raw lat/lon stays in
  the bronze and silver rows so nothing is lossy.

## Consequences

- **Resolution is a real trade-off, and it differs per source.** Res 7 for FIRMS detections is
  right; res 7 for Sentinel zonal statistics would mean 200k+ hexagons per region and is not
  affordable on a free Earth Engine quota (see ADR 0005). Coarse sources get coarse cells.
- **Cells are not regions.** A cell can straddle a boundary; `region_id` is assigned separately
  and both are carried, so a query can use whichever is meaningful.
- **Ingestion must validate.** A malformed cell id silently poisons every downstream join, so
  `BRONZE_ENVELOPE` rejects any `h3_index` that is not a valid cell.

## Alternatives rejected

- **Raw lat/lon only** — every aggregation becomes spatial SQL, and "same place over time"
  becomes a fuzzy match.
- **Geohash** — rectangular cells with worse neighbour properties at high latitudes, which is
  precisely where our ice regions are.
- **S2 geometry** — richer, but heavier than needed and less convenient as a compact column.
- **Geospatial Postgres (PostGIS)** — would reduce the case for Turso for no benefit at this
  data volume, and the serving layer is read-mostly key/value shaped.
