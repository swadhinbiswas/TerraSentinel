# ADR 0005 — Sentinel aggregation grain: region series + coarse H3 change map

**Status:** accepted · implemented in `collectors/sentinel_gee_collector.py`

## Context

The original design said every observation gets an H3 index at resolution ~7–8, uniformly
across sources. For FIRMS point detections that is straightforward. For Sentinel it is not.

Measured against the actual study regions (`h3.geo_to_cells`, before paying for anything):

| Region | res 7 (~5.2 km²) | res 6 (~36 km²) | res 5 (~253 km²) |
|---|---|---|---|
| Iberian Peninsula | 203,485 | 29,067 | 946¹ |
| Romanian Carpathians | 46,507 | 6,642 | 946 |
| European Alps | 86,275 | 12,324 | 1,762 |
| Norway & Svalbard | 677,760 | 96,822 | 1,233² |

Zonal statistics at that grain mean one `reduceRegions` feature per hexagon. Res 7 is
200k-plus polygons per region in a single call, at 10 m source resolution — far beyond both
the free Earth Engine quota and "a few requests per run".

## Decision

Two grains, chosen for what they actually answer:

1. **Region grain** (`spatial_scope="region"`) — one multi-band composite stack per region and
   one `reduceRegion` call returning mean/stddev/min/max/count per window for the *entire*
   requested date range. This is the series the deforestation autoencoder consumes, and a
   two-year backfill costs a single request per region.
2. **Grid grain** (`spatial_scope="grid"`) — a year-over-year change value per H3 cell at
   **resolution 5**, chunked at 500 cells per `reduceRegions` call. This is the dashboard
   heatmap, and it is deliberately coarse.

Raw imagery never leaves Earth Engine; only extracted statistics are stored.

## Why

- **Cost is dominated by polygon count, not by data volume.** Going from res 5 to res 7 is a
  200× increase in reducer work for a map that will be rendered at a scale where res-5 cells
  are already smaller than a pixel.
- **A regional series is the scientifically correct unit here.** Deforestation signals are
  trends over a monitoring area, not per-hectare verdicts; monthly composites over a region
  are what an anomaly model can actually learn a baseline from.
- **Honesty about resolution.** A res-5 cell is ~253 km². Calling that a "deforestation
  detection" would overstate it; it is a change hotspot indicator, and the map will be labelled
  as such rather than implying per-pixel accuracy.

## Consequences

- The two grains have different schemas of meaning: region rows carry a full window series,
  grid rows carry a single recent-vs-baseline change. Both are in `BRONZE_SENTINEL`, with
  `spatial_scope` making the distinction explicit rather than inferring it from nulls.
- Grid rows have no pixel count of their own, so `observation_count` is nullable.
- Per-pixel H3 bucketing is not implemented and is not planned for the free tier. If finer
  spatial detail is ever needed, the correct move is a smaller region of interest or a
  change-detection algorithm inside Earth Engine — not more polygons.

## Alternatives rejected

- **Uniform res-7 H3 everywhere** — the original plan; it does not survive contact with the
  per-request cost of zonal statistics.
- **Sampling pixels and indexing them individually** — loses the region aggregate, inflates
  storage, and stores imagery derivatives that the free Hub tier is explicitly not for.
- **Sentinel Hub / Planetary Computer instead of GEE** — viable, and worth revisiting if
  commercial use is ever contemplated (GEE is free for research only), but it changes the
  auth model and adds a paid-tier boundary for exactly the workload above.

---

¹ measured on the Iberia bbox at res 5 (946), shown here as the comparison column baseline
² Norway & Svalbard combined at res 5 totals 1,233
