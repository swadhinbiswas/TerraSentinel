-- Gold: the sea-surface-temperature half of the map layer.
--
-- Marine heatwaves are a shared precursor for Mediterranean fire risk and Norwegian
-- glacier melt, which is why SST cells belong on the same map as the fire cells.

{{ config(materialized='table') }}

select
    'noaa_nsidc' as source_id,
    region_id,
    h3_index,
    any_value(latitude) as latitude,
    any_value(longitude) as longitude,
    month_start as period_start,
    'month' as period_grain,
    metric_type,
    'h3_res_5' as spatial_scope,
    avg(value) as value,
    null::double as high_confidence_count,
    null::double as baseline_mean,
    now() as updated_at
from {{ ref('stg_noaa') }}
where metric_type = 'sst_anomaly'
group by region_id, h3_index, month_start, metric_type
