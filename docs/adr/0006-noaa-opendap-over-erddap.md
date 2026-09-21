# ADR 0006 — NOAA/NSIDC source selection: what the free archives actually are

**Status:** accepted · implemented in `collectors/noaa_nsidc_collector.py`

## Context

The plan called for "NOAA/NSIDC ocean and ice data". Enumerating what is *actually reachable
and current* from a free, keyless HTTP client turned out to be the substantive work. Findings
from probing the archives directly:

| Candidate | Result |
|---|---|
| `coastwatch.pfeg.noaa.gov` ERDDAP (OISST v2.1) | connection times out |
| `upwell.pfeg.noaa.gov/erddap` | reachable, but 302-redirects to `coastwatch` → unusable |
| NCEI ERDDAP `ncdc_oisst_v2_avhrr_amsr_*` | live, but the archive slice covers **2002–2011 only** — stale for monitoring |
| NOAA PSL THREDDS `sst.day.anom.{year}.nc` | **current (through Sep 2026), daily, gridded** |
| NSIDC `G02135 … v3.0` CSVs | **404** — superseded by v4.0 |
| NSIDC `G02135 … v4.0` CSVs | live; `Area` column removed; a **1981–2010 climatology file** added |
| NCEI Climate at a Glance time series | live, keyless, but monthly and global-only |

## Decision

Three arms, all keyless:

1. **NOAA OISST v2.1 daily SST anomaly** over OPeNDAP from NOAA PSL, sampled at 1° for regional
   summaries.
2. **NSIDC Sea Ice Index v4.0** daily extent for both hemispheres.
3. **NSIDC 1981–2010 climatology** — a static per-day-of-year baseline with standard deviation,
   fetched only on backfill (it is immutable).

Marine SST anomalies are collected for the three *marine* regions (Iberia, Greece, Norway),
because marine heatwaves are a genuine precursor for both Mediterranean fire risk and
Norwegian glacier melt. The landlocked Alps region is covered by Sentinel-1 SAR instead.

## Why

- **The climatology file is better than a naive baseline.** "Same week last year" has a sample
  size of one. A 30-year daily normal with a standard deviation gives the ice model a proper
  z-score denominator for free, which directly improves the anomaly signal.
- **Reachability is a requirement, not a detail.** An archive that cannot be fetched without a
  redirect to an unreachable host is not usable by a scheduled job, however good the data is.
- **Sampling at 1° rather than 0.25°** keeps a two-year regional backfill around 10⁵ rows
  instead of 10⁶, with no loss of signal for a *regional* anomaly statistic.
- **Three grains are all in one schema.** Hemispheric rows carry `spatial_scope="hemispheric"`
  with the basin centre as a nominal coordinate, so nothing downstream mistakes a hemisphere-wide
  index for a point measurement.

## Consequences

- The SST arm needs `pydap` (the `[noaa]` extra) because OPeNDAP is not plain HTTP. It is a
  pure-Python dependency and the import is lazy, so the other arms do not require it.
- **Two upstream quirks are load-bearing and now guarded:**
  - OISST longitudes are published in **0–360**. A bbox crossing the prime meridian becomes two
    windows (`split_lon_window`), and longitudes are converted back to −180…180 on write.
  - The time axis is CF-encoded as **days since 1800-01-01**, not a Unix epoch. It is decoded by
    reading the `units` attribute, and an unrecognised form raises rather than guessing — a wrong
    epoch produces plausible dates that poison every join.
  - The same variable is 3-D on one endpoint and 4-D with a singleton depth axis on another. A
    rank-agnostic reader checks the element count before reshaping, because a misaligned read
    does not raise on its own — it returns fewer values and silently misaligns the axes.
- The v4.0 series has no ice *area* column, so `sea_ice_area` is not collected. Adding a metric
  later is a schema change, not a rewrite.

## Alternatives rejected

- **NCEI ERDDAP OISST** — the obvious first choice; rejected because 2002–2011 is not a
  monitoring dataset.
- **NOAA Climate at a Glance** — verified working and keyless, but monthly and global-only. Kept
  as a documented fallback rather than built, since the daily gridded anomaly is strictly better.
- **Downloading full OISST NetCDF files** — hundreds of megabytes per year to compute one
  regional mean. The OPeNDAP subset is the same data over the wire in kilobytes.
- **ERSST / GHRSST via THREDDS catalog scraping** — more moving parts for no additional signal
  at a regional grain.
