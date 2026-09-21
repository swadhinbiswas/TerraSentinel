-- Gold: the fire half of the map layer.
--
-- Split from a single combined spatial mart. That mart unioned FIRMS, Sentinel and SST
-- arms, so a missing Sentinel source removed the fire and SST cells too — the map went
-- blank for reasons unrelated to the data that was actually missing. Each source now
-- has its own mart, so an outage can only remove its own cells.

{{ config(materialized='table') }}

select
    'firms' as source_id,
    region_id,
    h3_index,
    latitude,
    longitude,
    observation_date as period_start,
    'day' as period_grain,
    'fire_detection_count' as metric_type,
    'h3_res_7' as spatial_scope,
    detection_count::double as value,
    high_confidence_count::double as high_confidence_count,
    null::double as baseline_mean,
    now() as updated_at
from {{ ref('int_fire_daily_h3') }}
