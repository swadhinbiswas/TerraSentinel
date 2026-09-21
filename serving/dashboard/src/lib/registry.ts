/**
 * The data catalog: one registry describing every table the serving database can hold.
 *
 * This is a single source of truth for three things that would otherwise drift:
 *
 *   1. **What the catalog page shows** — domain, source, units, meaning.
 *   2. **What the explorer is allowed to read.** Table names are interpolated into SQL,
 *      so the allowlist is a security boundary, not a convenience: `explorerRows` refuses
 *      any name not in here.
 *   3. **What "absent" means.** Three tables depend on the Sentinel backfill, which has
 *      not run. Recording that here means the UI explains the gap instead of silently
 *      showing nothing, which is the difference between a dataset and a dataset you
 *      can trust.
 */
import type { Client } from "@libsql/client/web";

export type Domain = "fire" | "cryosphere" | "marine" | "vegetation" | "operations";

export interface TableMeta {
  name: string;
  title: string;
  domain: Domain;
  description: string;
  /** Upstream source, and the licence it carries. */
  source: string;
  /** Column used for "how fresh is this" and for date filtering. */
  dateColumn: string | null;
  /** Roughly what a row means, so a reader can interpret counts. */
  grain: string;
  /** Populated by our own pipeline rather than an upstream provider. */
  internal?: boolean;
  /** True when the table carries a `region_id`, so the explorer can offer a filter.
   *  Declared rather than probed: a wrong guess becomes invalid SQL, and this type is
   *  the allowlist that makes the explorer safe. */
  hasRegion?: boolean;
}

export const TABLE_REGISTRY: TableMeta[] = [
  {
    name: "gold_fire_anomalies",
    hasRegion: true,
    title: "Fire anomalies by region and day",
    domain: "fire",
    description:
      "Daily fire detections per study region, scored against the region's own ±15-day seasonal baseline using a median and scaled median-absolute-deviation. Includes context features: night-time share, cell concentration, FRP per detection, satellite count.",
    source: "NASA FIRMS — MODIS and VIIRS active fire products",
    dateColumn: "observation_date",
    grain: "one row per region per day, including zero-detection days",
  },
  {
    name: "gold_h3_fire",
    hasRegion: true,
    title: "Fire detections per H3 cell",
    domain: "fire",
    description:
      "Detections aggregated into H3 resolution-7 hexagons (~5.2 km²). This is the map layer: sparse by design, because an absent cell means no detection rather than zero activity in a cell that was observed.",
    source: "NASA FIRMS",
    dateColumn: "period_start",
    grain: "one row per H3 cell per day, only where detections exist",
  },
  {
    name: "gold_ice_extent_trends",
    hasRegion: true,
    title: "Sea-ice extent vs the 1981–2010 normal",
    domain: "cryosphere",
    description:
      "Daily hemispheric sea-ice extent scored against the published NSIDC 1981–2010 per-day-of-year normal and its own standard deviation. The strongest baseline in the pipeline: 30 years, published, needing no estimation from our rows.",
    source: "NSIDC Sea Ice Index v4.0",
    dateColumn: "period_start",
    grain: "one row per hemisphere per day",
  },
  {
    name: "gold_h3_sst",
    hasRegion: true,
    title: "Sea-surface temperature anomaly per H3 cell",
    domain: "marine",
    description:
      "Monthly mean sea-surface temperature anomaly, sampled at 1° and bucketed into H3 resolution-5 cells (~253 km²). Marine heatwaves are a shared precursor for Mediterranean fire risk and Norwegian glacier melt, which is why these cells belong on the same map as the fire cells.",
    source: "NOAA OISST v2.1 (via NOAA PSL OPeNDAP)",
    dateColumn: "period_start",
    grain: "one row per H3 cell per month",
  },
  {
    name: "gold_deforestation_index",
    hasRegion: true,
    title: "Deforestation index (NDVI, year-over-year)",
    domain: "vegetation",
    description:
      "Monthly mean NDVI per region with the same month a year earlier alongside, scored against that region's own distribution of year-over-year changes. A vegetation loss shows up as a negative z-score, so the anomaly test is on the lower tail.",
    source: "Copernicus Sentinel-2 via Google Earth Engine",
    dateColumn: "month_start",
    grain: "one row per region per month",
  },
  {
    name: "gold_glacier_backscatter",
    hasRegion: true,
    title: "Glacier SAR backscatter trend",
    domain: "cryosphere",
    description:
      "Monthly Sentinel-1 VV backscatter per glacier region, scored against its own year-over-year change distribution. No published climatology exists for backscatter, so this baseline is weaker than the sea-ice arm's and `baseline_source` says so rather than implying equivalence.",
    source: "Copernicus Sentinel-1 via Google Earth Engine",
    dateColumn: "period_start",
    grain: "one row per region per month",
  },
  {
    name: "gold_h3_sentinel",
    hasRegion: true,
    title: "Vegetation and glacier change per H3 cell",
    domain: "vegetation",
    description:
      "Year-over-year NDVI or SAR change per H3 resolution-5 cell (~253 km²). Deliberately coarse: bucketing Sentinel at resolution 7 would mean 200k+ polygons per region in a single Earth Engine call, which no free quota absorbs.",
    source: "Copernicus Sentinel-1/-2 via Google Earth Engine",
    dateColumn: "period_start",
    grain: "one row per H3 cell per composite window",
  },
  {
    name: "ml_predictions",
    hasRegion: true,
    title: "Model anomaly scores",
    domain: "operations",
    description:
      "Isolation Forest percentile score per region per day, ranked against the model's own training distribution so a percentile means the same thing now as it did at training time. Written by the batch scoring job, never by the transform.",
    source: "TerraSentinel ML layer",
    dateColumn: "observation_date",
    grain: "one row per region per day per model version",
    internal: true,
  },
  {
    name: "pipeline_runs",
    title: "Workflow run history",
    domain: "operations",
    description:
      "One row per scheduled workflow execution: status, row counts, duration, and the GitHub run it came from. This is what the dashboard's freshness indicators read, so pipeline health is answered from data rather than from an uptime ping.",
    source: "TerraSentinel workflows",
    dateColumn: "started_at",
    grain: "one row per workflow run",
    internal: true,
  },
  {
    name: "sources",
    title: "Source registry",
    domain: "operations",
    description:
      "Upstream sources with their cadence, attribution text and credential presence. The dashboard footer renders from this, so a licence attribution cannot drift out of date.",
    source: "TerraSentinel configuration",
    dateColumn: null,
    grain: "one row per source",
    internal: true,
  },
];

export const TABLE_INDEX = new Map(TABLE_REGISTRY.map((meta) => [meta.name, meta]));

/** Which upstream backfill each table needs. Drives the "why is this empty" message. */
export const BLOCKED_BY: Record<string, string> = {
  gold_deforestation_index: "Sentinel-2 via Google Earth Engine",
  gold_glacier_backscatter: "Sentinel-1 via Google Earth Engine",
  gold_h3_sentinel: "Sentinel-1/-2 via Google Earth Engine",
};

export interface TableStatus extends TableMeta {
  present: boolean;
  rows: number | null;
  firstValue: string | null;
  lastValue: string | null;
  columns: string[];
  blockedBy?: string;
}

const NUMERIC_DATE = /^\d{4}-\d{2}-\d{2}/;

export async function tableStatus(client: Client): Promise<TableStatus[]> {
  const statuses: TableStatus[] = [];

  for (const meta of TABLE_REGISTRY) {
    const status: TableStatus = {
      ...meta,
      present: false,
      rows: null,
      firstValue: null,
      lastValue: null,
      columns: [],
      blockedBy: BLOCKED_BY[meta.name],
    };

    try {
      const count = await client.execute(`select count(*) as n from ${meta.name}`);
      status.present = true;
      status.rows = Number(count.rows[0]?.n ?? 0);

      const info = await client.execute(`pragma table_info(${meta.name})`);
      status.columns = info.rows.map((row) => String(row.name));

      if (meta.dateColumn) {
        const range = await client.execute(
          `select min(${meta.dateColumn}) as lo, max(${meta.dateColumn}) as hi from ${meta.name}`,
        );
        const lo = range.rows[0]?.lo;
        const hi = range.rows[0]?.hi;
        status.firstValue = lo ? String(lo).match(NUMERIC_DATE)?.[0] ?? String(lo) : null;
        status.lastValue = hi ? String(hi).match(NUMERIC_DATE)?.[0] ?? String(hi) : null;
      }
    } catch {
      // Absent table: reported as absent rather than throwing, so one missing source
      // cannot blank the catalog.
    }

    statuses.push(status);
  }
  return statuses;
}
