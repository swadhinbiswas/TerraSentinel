-- A timestamp in the future is never a real observation: it means a timezone was
-- applied twice, a naive clock leaked in, or a source ships local time. Any of
-- those silently corrupts every seasonal join downstream, so it fails here.

select
    'stg_firms' as model_name,
    max(observation_date) as latest
from {{ ref('stg_firms') }}
having max(observation_date) > current_date + 1

union all

select
    'stg_noaa' as model_name,
    max(observation_date) as latest
from {{ ref('stg_noaa') }}
having max(observation_date) > current_date + 1
