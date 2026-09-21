-- Gold: monthly deforestation index per region.
--
-- The signal is a *drop* in NDVI relative to the same month a year earlier. The
-- z-score is against the distribution of that region's own year-over-year changes,
-- so a region with naturally variable NDVI does not flag every month.
--
-- A negative z-score is the interesting direction here, which is why the threshold
-- is negative and the severity ladder is inverted relative to the fire mart.

{{ config(materialized='table') }}

{% set drop_threshold = var('ndvi_drop_zscore_threshold', -2.0) %}
{#- NDVI changes are small; a near-zero spread would divide by almost nothing. -#}
{% set stddev_floor = 0.01 %}

with monthly as (

    select
        region_id,
        month_start,
        value_mean as ndvi_mean,
        value_stddev as ndvi_stddev,
        value_min as ndvi_min,
        value_max as ndvi_max,
        value_prior_year as ndvi_prior_year,
        value_change as ndvi_change,
        observation_count,
        cloud_pct_mean
    from {{ ref('int_sentinel_monthly_region') }}
    where metric_type = 'ndvi'

),

change_stats as (

    select
        region_id,
        avg(ndvi_change) as change_mean,
        stddev_samp(ndvi_change) as change_stddev,
        count(*) as change_samples
    from monthly
    where ndvi_change is not null
    group by 1

),

scored as (

    select
        m.*,
        s.change_mean,
        s.change_stddev,
        s.change_samples,
        case
            when m.ndvi_change is not null
                then (m.ndvi_change - s.change_mean)
                     / greatest(coalesce(s.change_stddev, 0), {{ stddev_floor }})
        end as change_zscore
    from monthly m
    left join change_stats s using (region_id)

)

select
    region_id,
    month_start,
    ndvi_mean,
    ndvi_stddev,
    ndvi_min,
    ndvi_max,
    ndvi_prior_year,
    ndvi_change,
    change_mean,
    change_stddev,
    change_samples,
    change_zscore,
    case
        when change_zscore is null then null
        else change_zscore <= {{ drop_threshold }}
    end as is_anomaly,
    case
        when change_zscore is null then 'unknown'
        when change_zscore <= -4 then 'extreme'
        when change_zscore <= -3 then 'high'
        when change_zscore <= {{ drop_threshold }} then 'moderate'
        else 'normal'
    end as severity,
    observation_count,
    cloud_pct_mean,
    cast(null as double) as anomaly_score,
    cast(null as varchar) as model_version,
    cast(null as timestamptz) as scored_at,
    now() as updated_at
from scored
