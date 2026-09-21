-- NOAA OISST sea-surface temperature anomalies and NSIDC sea-ice series.
--
-- Three metric types share this table:
--   sea_ice_extent             daily, hemispheric (one number per pole per day)
--   sea_ice_extent_climatology the static 1981-2010 per-day-of-year normal, which
--                              is the seasonal baseline the ice model divides by
--   sst_anomaly                daily gridded SST anomaly over the marine regions,
--                              used as a precursor signal for fire and ice melt
--
-- `spatial_scope` is carried through rather than inferred: a hemispheric index row
-- has a nominal coordinate, not a measurement location, and treating the two as
-- interchangeable would corrupt any spatial aggregation.

-- Materialisation comes from dbt_project.yml, not from here: staging is a
-- table because these read remote parquet (see the note in dbt_project.yml).

with source as (

    select * from {{ bronze_parquet('noaa_nsidc') }}

),

typed as (

    select
        try_cast(timestamp as timestamptz) as observed_at,
        cast(latitude as double) as latitude,
        cast(longitude as double) as longitude,
        h3_index,
        region_id,
        metric_type,
        product,
        coalesce(spatial_scope, 'gridded') as spatial_scope,
        cast(value as double) as value,
        unit,
        cast(baseline_stddev as double) as baseline_stddev,
        try_cast(day_of_year as integer) as day_of_year,
        try_cast(ingested_at as timestamptz) as ingested_at,
        run_id
    from source
    where region_id is not null
      and try_cast(timestamp as timestamptz) is not null

),

deduplicated as (

    select
        *,
        row_number() over (
            partition by
                metric_type,
                region_id,
                observed_at,
                latitude,
                longitude,
                -- Climatology rows repeat a timestamp once per day-of-year.
                coalesce(day_of_year, -1)
            order by ingested_at desc
        ) as dedupe_rank
    from typed

)

select
    observed_at,
    timezone('UTC', observed_at)::date as observation_date,
    date_trunc('month', timezone('UTC', observed_at))::date as month_start,
    latitude,
    longitude,
    h3_index,
    region_id,
    metric_type,
    product,
    spatial_scope,
    value,
    unit,
    baseline_stddev,
    day_of_year,
    ingested_at,
    run_id
from deduplicated
where dedupe_rank = 1
