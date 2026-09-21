-- Gold: glacier SAR backscatter trend per region.
--
-- The other half of the former combined ice mart. No published climatology exists for
-- SAR backscatter, so the baseline is the region's own distribution of year-over-year
-- changes — a genuinely weaker comparison, which `baseline_source` states rather than
-- leaving a reader to assume the two arms are equivalent.
--
-- Requires the Sentinel source, so this mart is absent until the GEE backfill runs.

{{ config(materialized='table') }}

{% set ice_threshold = var('ice_zscore_threshold', 2.5) %}
{% set min_samples = var('min_baseline_samples', 10) %}
{% set scale_floor = 0.05 %}

with sar as (

    select
        region_id,
        month_start as period_start,
        metric_type,
        value_mean as value,
        value_change,
        latitude,
        longitude
    from {{ ref('int_sentinel_monthly_region') }}
    where metric_type = 'sar_backscatter'

),

change_stats as (

    select
        region_id,
        avg(value_change) as change_mean,
        stddev_samp(value_change) as change_stddev,
        count(*) as change_samples
    from sar
    where value_change is not null
    group by 1

)

select
    s.region_id,
    s.period_start,
    s.metric_type,
    'series_yoy' as baseline_source,
    s.value,
    c.change_mean as baseline_mean,
    c.change_stddev as baseline_stddev,
    c.change_samples as baseline_samples,
    s.latitude,
    s.longitude,
    case
        when s.value_change is not null and c.change_samples >= {{ min_samples }}
            then (s.value_change - c.change_mean)
                 / greatest(coalesce(c.change_stddev, 0), {{ scale_floor }})
    end as zscore,
    case
        when s.value_change is null or c.change_samples < {{ min_samples }} then null
        else abs((s.value_change - c.change_mean)
                 / greatest(coalesce(c.change_stddev, 0), {{ scale_floor }})) >= {{ ice_threshold }}
    end as is_anomaly,
    case
        when s.value_change is null or c.change_samples < {{ min_samples }} then 'unknown'
        when abs((s.value_change - c.change_mean)
                 / greatest(coalesce(c.change_stddev, 0), {{ scale_floor }})) >= 4 then 'extreme'
        when abs((s.value_change - c.change_mean)
                 / greatest(coalesce(c.change_stddev, 0), {{ scale_floor }})) >= 3 then 'high'
        when abs((s.value_change - c.change_mean)
                 / greatest(coalesce(c.change_stddev, 0), {{ scale_floor }})) >= {{ ice_threshold }} then 'moderate'
        else 'normal'
    end as severity,
    cast(null as double) as anomaly_score,
    cast(null as varchar) as model_version,
    cast(null as timestamptz) as scored_at,
    now() as updated_at
from sar s
left join change_stats c using (region_id)
