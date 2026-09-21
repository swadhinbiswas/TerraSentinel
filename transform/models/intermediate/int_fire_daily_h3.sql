-- Fire activity per H3 cell per day: the dashboard's spatial layer.
--
-- Deliberately sparse. A missing cell means "no detection in that ~5 km^2 hexagon
-- that day", and the map renders absence as absence rather than as a zero-valued
-- hexagon, which would be visually indistinguishable from a small fire.

select
    h3_index,
    region_id,
    observation_date,
    any_value(latitude) as latitude,
    any_value(longitude) as longitude,
    count(*) as detection_count,
    sum(frp) as frp_sum,
    max(frp) as frp_max,
    sum(case when confidence = 'high' then 1 else 0 end) as high_confidence_count,
    avg(confidence_pct) as confidence_pct_mean
from {{ ref('stg_firms') }}
group by 1, 2, 3
