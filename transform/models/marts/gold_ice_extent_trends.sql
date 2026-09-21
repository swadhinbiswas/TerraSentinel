-- Gold: sea-ice extent scored against the NSIDC 1981-2010 climatology.
--
-- Split out from the former combined "ice melt" mart. That mart unioned this arm with
-- the Sentinel SAR arm, which meant a missing Sentinel source removed the sea-ice
-- results too — data that was present, healthy, and had nothing to do with Sentinel.
-- A union in dbt requires every input to exist, so arms with different sources belong
-- in different marts.
--
-- The baseline here is the strongest in the pipeline: a published 30-year normal with
-- its own standard deviation, so the score needs no estimation from our own rows.

{{ config(materialized='table') }}

{% set ice_threshold = var('ice_zscore_threshold', 2.5) %}
{% set climatology_years = var('nsidc_climatology_years', 30) %}
{% set scale_floor = 0.05 %}

with extent as (

    select region_id, observation_date, value, latitude, longitude
    from {{ ref('stg_noaa') }}
    where metric_type = 'sea_ice_extent'

),

climatology as (

    select
        region_id,
        day_of_year,
        avg(value) as clim_mean,
        avg(coalesce(baseline_stddev, 0)) as clim_stddev
    from {{ ref('stg_noaa') }}
    where metric_type = 'sea_ice_extent_climatology'
    group by 1, 2

),

scored as (

    select
        e.region_id,
        e.observation_date as period_start,
        'sea_ice_extent' as metric_type,
        'nsidc_climatology' as baseline_source,
        e.value,
        c.clim_mean as baseline_mean,
        c.clim_stddev as baseline_stddev,
        {{ climatology_years }} as baseline_samples,
        e.latitude,
        e.longitude,
        case
            when c.clim_mean is not null
                then (e.value - c.clim_mean) / greatest(coalesce(c.clim_stddev, 0), {{ scale_floor }})
        end as zscore
    from extent e
    left join climatology c
        on e.region_id = c.region_id
       and dayofyear(e.observation_date) = c.day_of_year

)

select
    region_id,
    period_start,
    metric_type,
    baseline_source,
    value,
    baseline_mean,
    baseline_stddev,
    baseline_samples,
    latitude,
    longitude,
    zscore,
    case
        when zscore is null then null
        -- Melt and freeze both matter, so this is a two-sided test.
        else abs(zscore) >= {{ ice_threshold }}
    end as is_anomaly,
    case
        when zscore is null then 'unknown'
        when abs(zscore) >= 4 then 'extreme'
        when abs(zscore) >= 3 then 'high'
        when abs(zscore) >= {{ ice_threshold }} then 'moderate'
        else 'normal'
    end as severity,
    cast(null as double) as anomaly_score,
    cast(null as varchar) as model_version,
    cast(null as timestamptz) as scored_at,
    now() as updated_at
from scored
