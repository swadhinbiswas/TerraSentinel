/**
 * Every read the dashboard performs, in one place.
 *
 * All of these are single indexed SQL reads against precomputed tables — no
 * aggregation, no joins across large sets, no inference. Pages Functions have a short
 * CPU budget on the free tier, and that budget is the reason batch scoring happens in
 * GitHub Actions rather than on the request path.
 *
 * Absent tables are handled rather than thrown: three marts depend on Sentinel data
 * that is not backfilled yet, and a missing table must degrade its own panel instead
 * of 500-ing the page.
 */
import type { Client } from "@libsql/client/web";
import { TABLE_INDEX, tableStatus, type TableStatus } from "@/lib/registry";
import { guardSql, MAX_ROWS } from "@/lib/sql-guard";

export interface AnomalyRow {
  region_id: string;
  observation_date: string;
  detection_count: number;
  zscore: number | null;
  severity: string;
  is_anomaly: number | null;
  anomaly_percentile: number | null;
}

export interface IceRow {
  region_id: string;
  period_start: string;
  value: number;
  baseline_mean: number | null;
  baseline_source: string;
  zscore: number | null;
  severity: string;
  is_anomaly: number | null;
}

export interface MapCell {
  h3_index: string;
  region_id: string;
  latitude: number;
  longitude: number;
  period_start: string;
  metric_type: string;
  spatial_scope: string;
  value: number;
}

export interface HealthReport {
  ok: boolean;
  database: { reachable: boolean; error?: string };
  tables: { name: string; present: boolean; rows: number | null }[];
  last_runs: { workflow: string; status: string; started_at: string; rows_written: number }[];
  model: { version: string | null; scored_at: string | null; predictions: number | null };
}

const EXPECTED_TABLES = [
  "gold_fire_anomalies",
  "gold_ice_extent_trends",
  "gold_h3_fire",
  "gold_h3_sst",
  // Sentinel-dependent: legitimately absent until the GEE backfill runs.
  "gold_deforestation_index",
  "gold_glacier_backscatter",
  "gold_h3_sentinel",
] as const;

export async function tableExists(client: Client, table: string): Promise<boolean> {
  const result = await client.execute({
    sql: "select 1 as present from sqlite_master where type = 'table' and name = ? limit 1",
    args: [table],
  });
  return result.rows.length > 0;
}

export async function anomalies(
  client: Client,
  options: { sinceDays?: number; region?: string; onlyAnomalies?: boolean } = {},
): Promise<AnomalyRow[]> {
  const { sinceDays = 400, region, onlyAnomalies = false } = options;
  const window = windowModifier(sinceDays);
  // Every column is qualified: `observation_date` exists on both sides of the join, and
  // an unqualified reference is an error rather than a guess.
  const clauses = ["a.observation_date >= date('now', ?)"];
  const args: (string | number)[] = [window];

  if (region) {
    clauses.push("a.region_id = ?");
    args.push(region);
  }
  if (onlyAnomalies) {
    clauses.push("a.is_anomaly = 1");
  }

  const result = await client.execute({
    sql: `select a.region_id, a.observation_date, a.detection_count, a.zscore, a.severity,
                 a.is_anomaly, p.anomaly_percentile
          from gold_fire_anomalies a
          left join ml_predictions p
            on p.region_id = a.region_id
           and p.observation_date = a.observation_date
          where ${clauses.join(" and ")}
          order by a.observation_date`,
    args,
  });
  return result.rows as unknown as AnomalyRow[];
}

export async function regions(client: Client): Promise<string[]> {
  const result = await client.execute(
    "select distinct region_id from gold_fire_anomalies order by region_id",
  );
  return result.rows.map((row) => String(row.region_id));
}

/** Coerce a caller-supplied window into a modifier SQLite will actually accept.
 *
 * This guards a genuinely silent failure: `date('now', '-[object Object] days')`
 * evaluates to NULL, and `x >= NULL` is NULL, so the query returns zero rows with no
 * error at all. A panels-worth of empty data is a much worse outcome than a thrown
 * exception, so a non-numeric window is rejected here.
 */
function windowModifier(sinceDays: unknown, fallback = 400): string {
  const days = typeof sinceDays === "number" && Number.isFinite(sinceDays) ? sinceDays : fallback;
  return `-${Math.abs(Math.trunc(days))} days`;
}

export async function iceTrends(
  client: Client,
  options: { sinceDays?: number } = {},
): Promise<IceRow[]> {
  if (!(await tableExists(client, "gold_ice_extent_trends"))) return [];
  const result = await client.execute({
    sql: `select region_id, period_start, value, baseline_mean, baseline_source,
                 zscore, severity, is_anomaly
          from gold_ice_extent_trends
          where period_start >= date('now', ?)
          order by period_start`,
    args: [windowModifier(options.sinceDays)],
  });
  return result.rows as unknown as IceRow[];
}

export interface MapWindow {
  layer: string;
  from: string;
  to: string;
  mode: "all" | "anomalies";
  cells: MapCell[];
  /** Quantile breaks of the visible values, so the legend can be data-driven rather
   *  than hardcoded. A fixed 1/10/100/1000 scale renders a typical cell (value ~3) as
   *  near-black on a dark basemap — which is exactly why the map looked empty. */
  breaks: number[];
  domain: { min: number; max: number };
  /** Cells in the window before the display cap, so "capped" is quantifiable. */
  totalCells: number;
  /** Region the window is scoped to, or null for all. */
  region: string | null;
  peak: MapCell | null;
  activity: { period_start: string; cells: number; peak: number }[];
  truncated: boolean;
}

/**
 * Cells for a date window, optionally restricted to days the detector flagged.
 *
 * The `anomalies` mode is the one that matters for a map: the record's biggest events are
 * historical (a 13,329-detection day in August 2025), so a "last N days" window
 * systematically shows the quiet tail and none of the signal.
 */
export async function mapWindow(
  client: Client,
  options: {
    layer?: string;
    from?: string;
    to?: string;
    mode?: "all" | "anomalies";
    region?: string;
    limit?: number;
  } = {},
): Promise<MapWindow> {
  const layer = options.layer === "sst" ? "sst" : "fire";
  const table = layer === "sst" ? "gold_h3_sst" : "gold_h3_fire";
  const limit = Math.min(Math.max(options.limit ?? 6000, 1), 20000);
  const mode = options.mode === "anomalies" ? "anomalies" : "all";

  const bounds = await client.execute(`select min(period_start) as lo, max(period_start) as hi from ${table}`);
  const from = options.from ?? String(bounds.rows[0]?.lo ?? "1970-01-01").slice(0, 10);
  const to = options.to ?? String(bounds.rows[0]?.hi ?? "2100-01-01").slice(0, 10);

  const anomalyJoin =
    mode === "anomalies"
      ? `join gold_fire_anomalies a
             on a.region_id = h.region_id and a.observation_date = h.period_start
                and a.is_anomaly = 1`
      : "";

  // Region scoping matters for legibility as much as for filtering: a window covering
  // both study regions spans 38 degrees of longitude, and at that scale individual cells
  // are invisible. Scoped to one region the same week frames at a zoom where the data
  // reads.
  const regionClause = options.region ? "and h.region_id = ?" : "";
  const regionArgs = options.region ? [options.region] : [];

  const cells = await client.execute({
    sql: `select h.h3_index, h.region_id, h.latitude, h.longitude, h.period_start,
                 h.metric_type, h.spatial_scope, h.value
          from ${table} h ${anomalyJoin}
          where h.period_start between ? and ? ${regionClause}
          order by h.value desc
          limit ?`,
    args: [from, to, ...regionArgs, limit],
  });

  const rows = cells.rows as unknown as MapCell[];

  // Quantiles are computed over the *entire* filtered set, not over the capped page.
  // Taking them from the returned rows would bias the scale: the page is ordered by value
  // descending, so a capped window would report its own top slice as the distribution —
  // a legend claiming a median of 16 when the real median is 3 is worse than no legend.
  const stats = await client.execute({
    sql: `select min(value) as min_v, max(value) as max_v, count(*) as n,
                 max(case when rn = cast(n * 0.50 as integer) + 1 then value end) as p50,
                 max(case when rn = cast(n * 0.75 as integer) + 1 then value end) as p75,
                 max(case when rn = cast(n * 0.90 as integer) + 1 then value end) as p90,
                 max(case when rn = cast(n * 0.98 as integer) + 1 then value end) as p98
          from (
            select h.value, row_number() over (order by h.value) as rn, count(*) over () as n
            from ${table} h ${anomalyJoin}
            where h.period_start between ? and ? ${regionClause}
          )`,
    args: [from, to, ...regionArgs],
  });
  const summary = stats.rows[0] ?? {};
  const num = (key: string, fallback: number): number => {
    const value = summary[key];
    return value === null || value === undefined ? fallback : Number(value);
  };
  const breaks = [
    num("p50", 1),
    num("p75", 2),
    num("p90", 3),
    num("p98", 4),
  ];

  const activity = await client.execute({
    sql: `select h.period_start, count(*) as cells, max(h.value) as peak
          from ${table} h ${anomalyJoin}
          where h.period_start between ? and ? ${regionClause}
          group by 1 order by 1`,
    args: [from, to, ...regionArgs],
  });

  return {
    layer,
    from,
    to,
    mode,
    region: options.region ?? null,
    cells: rows,
    breaks,
    domain: { min: num("min_v", 0), max: num("max_v", 0) },
    totalCells: num("n", rows.length),
    peak: rows[0] ?? null,
    activity: activity.rows as unknown as MapWindow["activity"],
    truncated: num("n", 0) > rows.length,
  };
}

export async function mapCells(
  client: Client,
  options: { layer?: string; sinceDays?: number; minValue?: number; limit?: number } = {},
): Promise<MapCell[]> {
  const { layer = "fire", sinceDays = 30, minValue = 1, limit = 4000 } = options;
  const table = layer === "sst" ? "gold_h3_sst" : "gold_h3_fire";
  if (!(await tableExists(client, table))) return [];

  const result = await client.execute({
    sql: `select h3_index, region_id, latitude, longitude, period_start, metric_type,
                 spatial_scope, value
          from ${table}
          where period_start >= date('now', ?) and value >= ?
          order by value desc
          limit ?`,
    args: [windowModifier(sinceDays), minValue, limit],
  });
  return result.rows as unknown as MapCell[];
}

export async function health(client: Client): Promise<HealthReport> {
  const report: HealthReport = {
    ok: false,
    database: { reachable: false },
    tables: [],
    last_runs: [],
    model: { version: null, scored_at: null, predictions: null },
  };

  try {
    await client.execute("select 1");
    report.database.reachable = true;
  } catch (error) {
    report.database.error = error instanceof Error ? error.message : String(error);
    return report;
  }

  for (const name of EXPECTED_TABLES) {
    // `count(*)` on a present table is cheap; on an absent one it throws, which is
    // exactly the signal being reported.
    try {
      const result = await client.execute(`select count(*) as n from ${name}`);
      report.tables.push({ name, present: true, rows: Number(result.rows[0]?.n ?? 0) });
    } catch {
      report.tables.push({ name, present: false, rows: null });
    }
  }

  try {
    const runs = await client.execute(
      `select workflow, status, started_at, rows_written
       from pipeline_runs order by started_at desc limit 5`,
    );
    report.last_runs = runs.rows as unknown as HealthReport["last_runs"];
  } catch {
    /* pipeline_runs is written by the workflows; absence is reported as empty */
  }

  try {
    const model = await client.execute(
      `select model_version, max(scored_at) as scored_at, count(*) as n
       from ml_predictions group by model_version order by scored_at desc limit 1`,
    );
    if (model.rows.length > 0) {
      report.model = {
        version: model.rows[0].model_version as string,
        scored_at: model.rows[0].scored_at as string,
        predictions: Number(model.rows[0].n ?? 0),
      };
    }
  } catch {
    /* no model scores yet */
  }

  report.ok =
    report.database.reachable &&
    report.tables.some((table) => table.present && table.name === "gold_fire_anomalies");
  return report;
}


// --------------------------------------------------------------------------
// Catalog, explorer, ops and the SQL console
// --------------------------------------------------------------------------

export type { TableStatus };

export async function catalog(client: Client): Promise<TableStatus[]> {
  return tableStatus(client);
}

export interface ExplorerPage {
  table: string;
  columns: string[];
  rows: Record<string, unknown>[];
  total: number;
  limit: number;
  offset: number;
}

/** Browse one registered table. The name is validated against the registry, because it
 *  is interpolated into SQL and that makes this function a trust boundary. */
export async function explorerPage(
  client: Client,
  options: { table: string; limit?: number; offset?: number; region?: string; sinceDays?: number },
): Promise<ExplorerPage> {
  const meta = TABLE_INDEX.get(options.table);
  if (!meta) {
    throw new Error(`table '${options.table}' is not in the catalog and cannot be browsed`);
  }

  const limit = Math.min(Math.max(options.limit ?? 50, 1), MAX_ROWS);
  const offset = Math.max(options.offset ?? 0, 0);

  const clauses: string[] = [];
  const args: (string | number)[] = [];
  if (options.region && meta.hasRegion) {
    clauses.push("region_id = ?");
    args.push(options.region);
  }
  if (options.sinceDays && meta.dateColumn) {
    clauses.push(`${meta.dateColumn} >= date('now', ?)`);
    args.push(`-${Math.abs(Math.trunc(options.sinceDays))} days`);
  }
  const where = clauses.length ? `where ${clauses.join(" and ")}` : "";
  const order = meta.dateColumn ? `order by ${meta.dateColumn} desc` : "";

  const total = await client.execute({
    sql: `select count(*) as n from ${meta.name} ${where}`,
    args,
  });

  const page = await client.execute({
    sql: `select * from ${meta.name} ${where} ${order} limit ? offset ?`,
    args: [...args, limit, offset],
  });

  return {
    table: meta.name,
    columns: [...page.columns],
    rows: page.rows as unknown as Record<string, unknown>[],
    total: Number(total.rows[0]?.n ?? 0),
    limit,
    offset,
  };
}

export interface OpsReport {
  runs: { workflow: string; status: string; started_at: string; duration_s: number | null; rows_written: number; failed_units: number }[];
  freshness: { name: string; title: string; lastValue: string | null; daysBehind: number | null; present: boolean }[];
  runsByStatus: { status: string; n: number }[];
  model: { version: string | null; scored_at: string | null; predictions: number | null };
  pageSize: { tables: number; rows: number };
}

export async function opsReport(client: Client): Promise<OpsReport> {
  const report: OpsReport = {
    runs: [],
    freshness: [],
    runsByStatus: [],
    model: { version: null, scored_at: null, predictions: null },
    pageSize: { tables: 0, rows: 0 },
  };

  try {
    const runs = await client.execute(
      `select workflow, status, started_at, duration_s, rows_written, failed_units
       from pipeline_runs order by started_at desc limit 25`,
    );
    report.runs = runs.rows as unknown as OpsReport["runs"];

    const byStatus = await client.execute(
      `select status, count(*) as n from pipeline_runs group by status order by n desc`,
    );
    report.runsByStatus = byStatus.rows as unknown as OpsReport["runsByStatus"];
  } catch {
    /* first run on a fresh database: no history yet */
  }

  const statuses = await tableStatus(client);
  report.freshness = statuses
    .filter((status) => status.dateColumn)
    .map((status) => {
      let daysBehind: number | null = null;
      if (status.lastValue) {
        const then = Date.parse(`${status.lastValue}T00:00:00Z`);
        if (!Number.isNaN(then)) {
          daysBehind = Math.max(0, Math.floor((Date.now() - then) / 86_400_000));
        }
      }
      return {
        name: status.name,
        title: status.title,
        lastValue: status.lastValue,
        daysBehind,
        present: status.present,
      };
    });

  report.pageSize = {
    tables: statuses.filter((status) => status.present).length,
    rows: statuses.reduce((sum, status) => sum + (status.rows ?? 0), 0),
  };

  try {
    const model = await client.execute(
      `select model_version, max(scored_at) as scored_at, count(*) as n
       from ml_predictions group by model_version order by scored_at desc limit 1`,
    );
    if (model.rows.length > 0) {
      report.model = {
        version: model.rows[0].model_version as string,
        scored_at: model.rows[0].scored_at as string,
        predictions: Number(model.rows[0].n ?? 0),
      };
    }
  } catch {
    /* no scores yet */
  }

  return report;
}

export interface QueryResult {
  columns: string[];
  rows: Record<string, unknown>[];
  truncated: boolean;
  elapsedMs: number;
}

export async function runGuardedQuery(client: Client, sql: string): Promise<QueryResult> {
  const verdict = guardSql(sql);
  if (!verdict.ok || !verdict.sql) {
    throw new Error(verdict.reason ?? "query rejected");
  }

  const started = Date.now();
  const result = await client.execute(verdict.sql);
  return {
    columns: [...result.columns],
    rows: result.rows as unknown as Record<string, unknown>[],
    truncated: result.rows.length >= MAX_ROWS,
    elapsedMs: Date.now() - started,
  };
}


// --------------------------------------------------------------------------
// Analysis
// --------------------------------------------------------------------------

export interface MonthProfileRow {
  region_id: string;
  month: number;
  mean_detections: number;
  flagged_days: number;
}

/** Mean detections and flagged-day count per calendar month, per region. */
export async function seasonalProfile(client: Client): Promise<MonthProfileRow[]> {
  // `month()` is DuckDB, not SQLite — the serving database needs strftime. The same
  // trap applies to date_trunc, dayofyear and the quantile family.
  const result = await client.execute(`
    select region_id,
           cast(strftime('%m', observation_date) as integer) as month,
           avg(detection_count) as mean_detections,
           sum(case when is_anomaly = 1 then 1 else 0 end) as flagged_days
    from gold_fire_anomalies
    group by 1, 2
    order by 1, 2
  `);
  return result.rows as unknown as MonthProfileRow[];
}

export interface RegimeComparison {
  regime: "flagged" | "unflagged";
  days: number;
  mean_detections: number;
  night_share: number | null;
  cell_concentration: number | null;
  frp_per_detection: number | null;
  satellites: number | null;
}

/** How flagged days differ from the rest, across the context features.
 *
 * A detector that only separates magnitude is not separating anything interesting; if
 * these columns look the same in both regimes, the extra features are decoration.
 */
export async function regimeComparison(client: Client): Promise<RegimeComparison[]> {
  const result = await client.execute(`
    select case when is_anomaly = 1 then 'flagged' else 'unflagged' end as regime,
           count(*) as days,
           avg(detection_count) as mean_detections,
           avg(night_detection_share) as night_share,
           avg(cell_concentration) as cell_concentration,
           avg(frp_per_detection) as frp_per_detection,
           avg(satellite_count) as satellites
    from gold_fire_anomalies
    where detection_count > 0
    group by 1
    order by 1 desc
  `);
  return result.rows as unknown as RegimeComparison[];
}

export interface RegionSummary {
  region_id: string;
  first_day: string;
  last_day: string;
  total_detections: number;
  peak: number;
  flagged_days: number;
  days: number;
  median: number | null;
}

export async function regionSummaries(client: Client): Promise<RegionSummary[]> {
  const result = await client.execute(`
    select region_id,
           min(observation_date) as first_day,
           max(observation_date) as last_day,
           sum(detection_count) as total_detections,
           max(detection_count) as peak,
           sum(case when is_anomaly = 1 then 1 else 0 end) as flagged_days,
           count(*) as days,
           median(detection_count) as median
    from gold_fire_anomalies
    group by 1 order by 1
  `);
  return result.rows as unknown as RegionSummary[];
}

export interface TopEvent {
  region_id: string;
  observation_date: string;
  detection_count: number;
  zscore: number | null;
  severity: string;
  night_detection_share: number | null;
  cell_concentration: number | null;
  frp_per_detection: number | null;
}

export async function topEvents(client: Client, limit = 15): Promise<TopEvent[]> {
  const result = await client.execute({
    sql: `select region_id, observation_date, detection_count, zscore, severity,
                 night_detection_share, cell_concentration, frp_per_detection
          from gold_fire_anomalies
          where zscore is not null
          order by zscore desc limit ?`,
    args: [limit],
  });
  return result.rows as unknown as TopEvent[];
}
