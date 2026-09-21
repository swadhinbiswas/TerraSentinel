-- Gold: daily fire anomaly per region.
--
-- Anomaly definition, stated in the open: how far a day's detection count sits
-- above the region's own +/-15 day seasonal baseline, in robust units. The baseline
-- is a median with a scaled median-absolute-deviation scale, so a single large fire
-- cannot inflate the yardstick it is measured against (see
-- int_seasonal_fire_baseline for the reasoning).
--
-- A model score is layered on separately (`anomaly_score`, filled by the batch
-- scoring job) so the statistical and learned signals can be compared rather than
-- conflated: `baseline_method` records how the statistical score was produced.
--
-- Thresholds come from project vars rather than being inlined, so "what counts as
-- an anomaly" is reviewable in one place.

{{ config(materialized='table') }}

{% set zscore_threshold = var('fire_zscore_threshold', 5.0) %}
{% set min_samples = var('min_baseline_samples', 10) %}
{#- A near-zero scale would make one detection look infinitely anomalous; flooring
    at half a detection keeps winter scores interpretable. -#}
{% set scale_floor = 0.5 %}

with daily as (

    select * from {{ ref('int_fire_daily_region') }}

),

baseline as (

    select * from {{ ref('int_seasonal_fire_baseline') }}

),

joined as (

    select
        d.region_id,
        d.observation_date,
        d.detection_count,
        d.h3_cell_count,
        d.high_confidence_count,
        d.frp_sum,
        d.frp_max,
        d.frp_mean,
        d.confidence_pct_mean,
        d.night_detection_count,
        d.satellite_count,
        d.max_cell_detections,
        b.baseline_median,
        b.baseline_mad,
        b.baseline_scale,
        b.baseline_samples,
        b.baseline_years
    from daily d
    left join baseline b
        on d.region_id = b.region_id
       and dayofyear(d.observation_date) = b.day_of_year

),

scored as (

    select
        *,
        case
            when baseline_samples >= {{ min_samples }}
                then (detection_count - baseline_median)
                     / greatest(coalesce(baseline_scale, 0), {{ scale_floor }})
        end as zscore
    from joined

)

select
    region_id,
    observation_date,
    detection_count,
    h3_cell_count,
    high_confidence_count,
    frp_sum,
    frp_max,
    frp_mean,
    confidence_pct_mean,
    -- Shares, not raw counts: a 5% night share means something different on a day with
    -- 20 detections than on a day with 13,000, and the raw count conflates the two.
    case
        when detection_count > 0
            then night_detection_count::double / detection_count
    end as night_detection_share,
    case
        when detection_count > 0
            then max_cell_detections::double / detection_count
    end as cell_concentration,
    case
        when detection_count > 0
            then frp_sum / detection_count
    end as frp_per_detection,
    night_detection_count,
    satellite_count,
    max_cell_detections,
    baseline_median,
    baseline_mad,
    baseline_scale,
    baseline_samples,
    baseline_years,
    'median_mad' as baseline_method,
    zscore,
    case
        when zscore is null then null
        else zscore >= {{ zscore_threshold }}
    end as is_anomaly,
    case
        when zscore is null then 'unknown'
        when zscore >= 12 then 'extreme'
        when zscore >= 8 then 'high'
        when zscore >= {{ zscore_threshold }} then 'moderate'
        else 'normal'
    end as severity,
    -- Populated by the ML batch scoring job; null means "not yet model-scored",
    -- which is distinct from "scored as normal".
    cast(null as double) as anomaly_score,
    cast(null as varchar) as model_version,
    cast(null as timestamptz) as scored_at,
    now() as updated_at
from scored
