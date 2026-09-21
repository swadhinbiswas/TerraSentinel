-- Seasonal fire baseline per region: median and MAD over a circular day-of-year window.
--
-- Two deliberate choices, both about not being fooled by the very events we are
-- trying to detect:
--
-- 1. **A +/-15 day window, not "the same date last year".** With two years of
--    history a same-date baseline has a sample size of two. Pooling the window
--    gives ~31 x years samples. The window wraps the year, so late December is
--    compared against early January rather than against nothing.
--
-- 2. **Median and MAD, not mean and standard deviation.** A single large fire day
--    sits inside its own window and, with mean/stddev, inflates the scale it is
--    then measured against — the outlier partly hides itself. The median ignores
--    it entirely, and the median absolute deviation scales robustly. The 1.4826
--    factor makes MAD a consistent estimator of sigma for normal data, so the
--    resulting score keeps the familiar "number of standard deviations" reading.

with daily as (

    select region_id, observation_date, detection_count
    from {{ ref('int_fire_daily_region') }}

),

offsets as (

    select unnest(range(-15, 16)) as day_offset

),

contributions as (

    select
        d.region_id,
        d.observation_date,
        ((dayofyear(d.observation_date) - 1 + o.day_offset + 366) % 366) + 1 as day_of_year,
        d.detection_count
    from daily d
    cross join offsets o

),

centres as (

    select
        region_id,
        day_of_year,
        median(detection_count) as baseline_median,
        count(*) as baseline_samples,
        count(distinct year(observation_date)) as baseline_years
    from contributions
    group by 1, 2

),

spreads as (

    select
        c.region_id,
        c.day_of_year,
        median(abs(c.detection_count - k.baseline_median)) as baseline_mad
    from contributions c
    join centres k
        on c.region_id = k.region_id
       and c.day_of_year = k.day_of_year
    group by 1, 2

)

select
    k.region_id,
    k.day_of_year,
    k.baseline_median,
    s.baseline_mad,
    -- Scaled MAD: a robust stand-in for the standard deviation.
    1.4826 * s.baseline_mad as baseline_scale,
    k.baseline_samples,
    k.baseline_years
from centres k
join spreads s
    on k.region_id = s.region_id
   and k.day_of_year = s.day_of_year
