-- Sentinel-2 NDVI and Sentinel-1 SAR statistics from Earth Engine.
--
-- Two spatial grains share this table, distinguished by `spatial_scope`:
--   region -- a multi-window time series over a whole study region, which is what
--             the deforestation model consumes;
--   grid   -- a single year-over-year change value per H3 cell, which is what the
--             dashboard map renders.
-- See docs/adr/0005-gee-aggregation-grain.md for why the grid is res 5, not res 7.

-- Materialisation comes from dbt_project.yml, not from here: staging is a
-- table because these read remote parquet (see the note in dbt_project.yml).

with source as (

    select * from {{ bronze_parquet('sentinel') }}

),

typed as (

    select
        try_cast(window_start as timestamptz) as window_start,
        try_cast(window_end as timestamptz) as window_end,
        cast(latitude as double) as latitude,
        cast(longitude as double) as longitude,
        h3_index,
        region_id,
        product,
        metric_type,
        coalesce(spatial_scope, 'region') as spatial_scope,
        cast(mean as double) as value_mean,
        cast(stddev as double) as value_stddev,
        cast("min" as double) as value_min,
        cast("max" as double) as value_max,
        try_cast(observation_count as bigint) as observation_count,
        cast(cloud_pct as double) as cloud_pct,
        cast(baseline_mean as double) as baseline_mean,
        cast(recent_mean as double) as recent_mean,
        try_cast(ingested_at as timestamptz) as ingested_at,
        run_id
    from source
    where region_id is not null
      and try_cast(window_end as timestamptz) is not null

),

deduplicated as (

    select
        *,
        row_number() over (
            partition by metric_type, spatial_scope, window_end, h3_index
            order by ingested_at desc
        ) as dedupe_rank
    from typed

)

select
    window_start,
    window_end,
    timezone('UTC', window_end)::date as period_end,
    date_trunc('month', timezone('UTC', window_end))::date as month_start,
    latitude,
    longitude,
    h3_index,
    region_id,
    product,
    metric_type,
    spatial_scope,
    value_mean,
    value_stddev,
    value_min,
    value_max,
    observation_count,
    cloud_pct,
    baseline_mean,
    recent_mean,
    ingested_at,
    run_id
from deduplicated
where dedupe_rank = 1
