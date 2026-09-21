-- Catches a region that is configured but silently absent from the lake — the
-- failure mode where a dashboard panel is simply empty and nobody notices.

select
    r.region_id
from {{ ref('regions') }} r
left join (
    select distinct region_id from {{ ref('int_fire_daily_region') }}
) d using (region_id)
where r.anomaly_type = 'fire'
  and d.region_id is null
