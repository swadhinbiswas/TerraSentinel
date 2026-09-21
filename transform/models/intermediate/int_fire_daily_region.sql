-- Dense region x day fire activity, with the context features the analysis needs.
--
-- The spine is the point of this model. Without it, a region-day with zero detections
-- simply does not exist, which is indistinguishable from "no data arrived" — and for a
-- fire model, "nothing burned" is the single most important negative example.
--
-- Beyond counts, three signals are worth separating because they mean different things:
--   * night share  — night-time detections point at different fuels and behaviour
--   * cell peak    — one dense cluster is a different event from many scattered fires
--   * FRP per detection — intensity, which does not scale with how many were seen

{{ config(tags=['fire']) }}

with fire_regions as (

    select region_id
    from {{ ref('regions') }}
    where anomaly_type = 'fire'

),

bounds as (

    select
        min(observation_date) as first_day,
        max(observation_date) as last_day
    from {{ ref('stg_firms') }}

),

spine as (

    select
        r.region_id,
        d.day::date as observation_date
    from fire_regions r
    cross join (
        select unnest(generate_series(first_day, last_day, interval 1 day)) as day
        from bounds
    ) d

),

detections as (

    select
        region_id,
        observation_date,
        count(*) as detection_count,
        count(distinct h3_index) as h3_cell_count,
        sum(frp) as frp_sum,
        max(frp) as frp_max,
        avg(frp) as frp_mean,
        sum(case when confidence = 'high' then 1 else 0 end) as high_confidence_count,
        avg(confidence_pct) as confidence_pct_mean,
        sum(case when daynight = 'N' then 1 else 0 end) as night_detection_count,
        count(distinct satellite) as satellite_count
    from {{ ref('stg_firms') }}
    group by 1, 2

),

cell_peaks as (

    -- Detections per hexagon per day, so the densest single cell can be reported
    -- separately from the regional total.
    select
        region_id,
        observation_date,
        max(cell_count) as max_cell_detections
    from (
        select region_id, observation_date, h3_index, count(*) as cell_count
        from {{ ref('stg_firms') }}
        group by 1, 2, 3
    )
    group by 1, 2

)

select
    s.region_id,
    s.observation_date,
    coalesce(d.detection_count, 0) as detection_count,
    coalesce(d.h3_cell_count, 0) as h3_cell_count,
    coalesce(d.high_confidence_count, 0) as high_confidence_count,
    coalesce(d.night_detection_count, 0) as night_detection_count,
    coalesce(d.satellite_count, 0) as satellite_count,
    coalesce(p.max_cell_detections, 0) as max_cell_detections,
    d.frp_sum,
    d.frp_max,
    d.frp_mean,
    d.confidence_pct_mean
from spine s
left join detections d
    on s.region_id = d.region_id
   and s.observation_date = d.observation_date
left join cell_peaks p
    on s.region_id = p.region_id
   and s.observation_date = p.observation_date
