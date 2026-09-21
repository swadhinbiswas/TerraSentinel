-- Gold: the Sentinel half of the map layer — year-over-year NDVI/SAR change per H3 cell.
--
-- Res 5 (~253 km^2) by design: bucketing Sentinel at res 7 would mean 200k+ polygons per
-- region in one Earth Engine call, which no free quota absorbs. `spatial_scope` records
-- the resolution so the UI cannot imply ~5 km^2 detail for a 253 km^2 cell.
--
-- Absent until the GEE backfill runs.

{{ config(materialized='table') }}

select
    'sentinel' as source_id,
    region_id,
    h3_index,
    latitude,
    longitude,
    period_end as period_start,
    'week' as period_grain,
    metric_type,
    'h3_res_5' as spatial_scope,
    value_mean as value,
    null::double as high_confidence_count,
    baseline_mean,
    now() as updated_at
from {{ ref('stg_sentinel') }}
where spatial_scope = 'grid'
