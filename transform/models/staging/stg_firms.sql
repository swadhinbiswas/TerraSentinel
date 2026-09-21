-- NASA FIRMS active fire detections, deduplicated and typed.
--
-- Bronze is append-only by design, so the same detection legitimately appears
-- several times: overlapping live windows re-fetch a day, and a single fire is
-- usually seen by more than one instrument. Deduplication belongs here rather
-- than at ingestion so that a re-run can never lose data.

-- Materialisation comes from dbt_project.yml, not from here: staging is a
-- table because these read remote parquet (see the note in dbt_project.yml).

with source as (

    select * from {{ bronze_parquet('firms') }}

),

typed as (

    select
        try_cast(acq_datetime as timestamptz) as observed_at,
        cast(latitude as double) as latitude,
        cast(longitude as double) as longitude,
        h3_index,
        region_id,
        product,
        confidence,
        cast(confidence_pct as double) as confidence_pct,
        cast(frp as double) as frp,
        -- MODIS reports band-21 brightness; VIIRS reports bright_ti4. One column
        -- downstream means the fire model never branches on instrument.
        coalesce(try_cast(bright_ti4 as double), try_cast(brightness as double)) as brightness_k,
        daynight,
        satellite,
        instrument,
        try_cast(ingested_at as timestamptz) as ingested_at,
        run_id
    from source
    where region_id is not null
      and try_cast(acq_datetime as timestamptz) is not null

),

deduplicated as (

    select
        *,
        row_number() over (
            partition by product, observed_at, latitude, longitude
            order by ingested_at desc
        ) as dedupe_rank
    from typed

)

select
    observed_at,
    timezone('UTC', observed_at)::date as observation_date,
    latitude,
    longitude,
    h3_index,
    region_id,
    product,
    confidence,
    confidence_pct,
    frp,
    brightness_k,
    daynight,
    satellite,
    instrument,
    ingested_at,
    run_id
from deduplicated
where dedupe_rank = 1
