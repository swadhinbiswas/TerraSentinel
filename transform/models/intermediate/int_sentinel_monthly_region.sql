-- Monthly Sentinel values per region, with the same month one year earlier alongside.
--
-- Handles NDVI and SAR uniformly: both are monthly region-grain composites, and for
-- both the meaningful comparison is year-over-year rather than month-over-month,
-- because a month-over-month delta is dominated by the seasonal cycle.
--
-- `lag(..., 12)` requires twelve consecutive months of history, so the first year
-- of a two-year backfill has a null prior value and is excluded from scoring rather
-- than scored against nothing.

with monthly as (

    select
        region_id,
        metric_type,
        month_start,
        avg(value_mean) as value_mean,
        stddev_samp(value_mean) as value_stddev,
        min(value_min) as value_min,
        max(value_max) as value_max,
        sum(observation_count) as observation_count,
        avg(cloud_pct) as cloud_pct_mean,
        count(*) as row_count,
        -- Constant for region-grain composites; carried so region-level marts do
        -- not have to re-join a point coordinate they already imply.
        any_value(latitude) as latitude,
        any_value(longitude) as longitude
    from {{ ref('stg_sentinel') }}
    where spatial_scope = 'region'
    group by 1, 2, 3

),

lagged as (

    select
        *,
        lag(value_mean, 12) over (
            partition by region_id, metric_type order by month_start
        ) as value_prior_year
    from monthly

)

select
    *,
    value_mean - value_prior_year as value_change
from lagged
