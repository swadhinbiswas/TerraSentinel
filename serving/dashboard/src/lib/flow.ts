import type { Client } from "@libsql/client/web";
import { currentModel } from "@/lib/queries";
import { BLOCKED_BY, martsBySource, tableStatus } from "@/lib/registry";

/**
 * The pipeline, as data.
 *
 * Every stage here is a real thing: the four crons, the two Hub datasets, the dbt build,
 * the Isolation Forest, the batch scorer and the Pages deployment are all named in
 * `.github/workflows/`. The stages are grouped to match how the data actually moves, not
 * to make the picture symmetrical.
 *
 * Two things are worth knowing about the shape of this:
 *
 *   * **Train and Score are one workflow.** `train_model.yml` is a single job whose steps
 *     are train → validate → publish → score, so there is no independent run to report for
 *     either. They are drawn as two stages because they answer different questions, and
 *     both read from the same run.
 *
 *   * **A stage can be `unrecorded`.** Every workflow is supposed to write a
 *     `pipeline_runs` row, and the training workflow says so in a comment and guards the
 *     step with `if: always()`. A stage with no rows is therefore reported as unrecorded
 *     rather than drawn as idle-and-fine, because the gap is the finding.
 */
export type StageId = "collect" | "store" | "transform" | "train" | "score" | "serve";

export type StageState = "live" | "overdue" | "failed" | "unrecorded" | "external";

export interface StageSpec {
  id: StageId;
  label: string;
  /** Where it runs, as a single phrase. */
  where: string;
  /** What it does, one sentence. */
  what: string;
  /** `pipeline_runs.workflow`, or null when the stage has no run of its own. */
  workflow: string | null;
  /** The cron, when there is one. */
  cron: string | null;
  /**
   * Hours after which a run is late. Fire data runs every six hours; a monthly composite
   * is not late for a month. One threshold for everything would either cry wolf on the
   * slow stages or hide a dead feed.
   */
  budgetHours: number | null;
  /** Whether this stage can be expected to have run at all yet. */
  optional?: boolean;
}

export const STAGES: StageSpec[] = [
  {
    id: "collect",
    label: "Collect",
    where: "GitHub Actions",
    what: "Four collectors pull FIRMS, Copernicus, NOAA/NSIDC and ENTSO-E. Backoff, a per-source circuit breaker and Pandera checks at ingress and egress.",
    workflow: "collect_data",
    cron: "15 */6, 30 3, 45 4 * * 1, 35 7",
    budgetHours: 8,
  },
  {
    id: "store",
    label: "Store",
    where: "Hugging Face Hub",
    what: "Bronze parquet in a git-backed dataset, so every landing is attributable to a commit. Partitioned by source, region and period.",
    workflow: "collect_data",
    cron: null,
    budgetHours: null,
  },
  {
    id: "transform",
    label: "Transform",
    where: "dbt on DuckDB",
    what: "staging → intermediate → gold, with the lake mirrored first because DuckDB re-lists a remote tree on every query.",
    workflow: "transform",
    cron: "30 5 * * *",
    budgetHours: 30,
  },
  {
    id: "train",
    label: "Train",
    where: "GitHub Actions",
    what:
      "Isolation Forest on strictly causal features. Every run is logged with the " +
      "dataset commit hash that produced it, in MLflow where it is installed and to a " +
      "run record where it is not, so a model is never published without the data behind " +
      "it. The bundle is published to a model repo with a generated model card.",
    workflow: "train_model",
    cron: "0 6 * * 1",
    budgetHours: 24 * 9,
  },
  {
    id: "score",
    label: "Score",
    where: "GitHub Actions",
    what: "The same job batch-scores the latest gold features and upserts percentiles. Nothing infers on the request path.",
    workflow: "train_model",
    cron: "0 6 * * 1",
    budgetHours: 24 * 9,
  },
  {
    id: "serve",
    label: "Serve",
    where: "Cloudflare Pages",
    what: "Astro routes read Turso over HTTP, which is what makes an edge runtime work without a proxy. This page is one of those queries.",
    workflow: null,
    cron: "on push",
    budgetHours: null,
  },
];

export interface StageRun {
  stage: StageSpec;
  state: StageState;
  lastRun: string | null;
  ageHours: number | null;
  durationS: number | null;
  rows: number | null;
  failedUnits: number | null;
  runs: number;
  successRate: number | null;
  /** Set when `state` is `unrecorded`, to say what is missing rather than just that it is. */
  note?: string;
}

export interface SourceFlow {
  id: string;
  label: string;
  cadence: string;
  metrics: number;
  h3Resolution: number;
  attribution: string;
  docsUrl: string | null;
  credentials: { name: string; present: boolean }[];
  blocked: boolean;
  /** The marts this source's data reaches, so a reader can tell "collected" from
   *  "on a page". Empty means the data is in the lake and nothing on the dashboard is
   *  built from it yet — a gap in the mart layer, and named as one. */
  servedBy: string[];
}

export interface FlowReport {
  stages: StageRun[];
  sources: SourceFlow[];
  /** One row per dated mart, with its freshness budget. */
  marts: {
    name: string;
    title: string;
    domain: string;
    rows: number | null;
    present: boolean;
    lastValue: string | null;
    daysBehind: number | null;
    budgetDays: number;
    blockedBy: string | null;
  }[];
  model: { version: string; scoredAt: string; predictions: number } | null;
  totals: { tables: number; tablesTotal: number; rows: number; runs: number; successRate: number | null };
  generatedAt: string;
}

const MART_BUDGET_DAYS: Record<string, number> = {
  gold_fire_anomalies: 3,
  gold_h3_fire: 3,
  gold_ice_extent_trends: 10,
  gold_h3_sst: 45,
  gold_deforestation_index: 45,
  gold_glacier_backscatter: 45,
  gold_h3_sentinel: 45,
  ml_predictions: 10,
  pipeline_runs: 30,
};

interface RunAggregate {
  workflow: string;
  runs: number;
  ok: number;
  last_run: string | null;
  last_ok: string | null;
  rows_total: number;
  failed_units: number;
  duration_s: number | null;
  last_status: string | null;
  last_rows: number | null;
}

function ageHours(iso: string | null): number | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return Math.max(0, (Date.now() - then) / 3_600_000);
}

export async function flowReport(client: Client): Promise<FlowReport> {
  // One pass over pipeline_runs: per-workflow totals, plus the most recent run of each so
  // duration and rows come from the same row the timestamp does.
  const runs = (await client.execute(`
    with ranked as (
      select workflow, status, started_at, duration_s, rows_written, failed_units,
             row_number() over (partition by workflow order by started_at desc) as rn
        from pipeline_runs
    )
    select totals.workflow, totals.runs, totals.ok, totals.last_run, totals.last_ok,
           totals.rows_total, totals.failed_units,
           latest.status as last_status, latest.duration_s, latest.rows_written as last_rows
      from (select workflow,
                   count(*) as runs,
                   sum(case when status = 'success' then 1 else 0 end) as ok,
                   max(started_at) as last_run,
                   max(case when status = 'success' then started_at end) as last_ok,
                   sum(rows_written) as rows_total,
                   sum(failed_units) as failed_units
              from pipeline_runs group by workflow) totals
      left join (select * from ranked where rn = 1) latest
        on latest.workflow = totals.workflow
  `)).rows as unknown as RunAggregate[];

  const byWorkflow = new Map(runs.map((run) => [run.workflow, run]));

  const stages: StageRun[] = STAGES.map((stage) => {
    // Two stages can share one workflow. Reporting the same row under both is honest;
    // inventing a second run for the other half would not be.
    const record = stage.workflow ? byWorkflow.get(stage.workflow) : undefined;
    const age = ageHours(record?.last_run ?? null);

    let state: StageState;
    let note: string | undefined;

    if (stage.id === "serve") {
      // The dashboard is running, which is the only evidence there is.
      state = "external";
    } else if (!stage.workflow) {
      state = "external";
    } else if (!record || record.runs === 0) {
      state = "unrecorded";
      note = `${stage.workflow}.yml records a run on every execution, so the absence of rows is a gap in the run history rather than a stage that has not been attempted.`;
    } else if (record.last_status && record.last_status !== "success") {
      state = "failed";
      note = `The most recent run finished ${String(record.last_status)}.`;
    } else if (stage.budgetHours !== null && age !== null && age > stage.budgetHours) {
      state = "overdue";
      note = `Last run was ${Math.round(age)} hours ago, against a ${stage.budgetHours} hour budget.`;
    } else {
      state = "live";
    }

    return {
      stage,
      state,
      lastRun: record?.last_run ?? null,
      ageHours: age,
      durationS: record?.duration_s ?? null,
      rows: record?.last_rows ?? null,
      failedUnits: record?.failed_units ?? null,
      runs: record?.runs ?? 0,
      successRate: record && record.runs > 0 ? record.ok / record.runs : null,
      note,
    };
  });

  // The source registry is written by the pipeline, so the credential state shown here is
  // the state the last run actually found rather than a hand-maintained list.
  // Inverted once, not per source: the registry is a module constant and the map is
  // built from it, so the cost is one pass over ten table definitions.
  const martsById = martsBySource();
  const sources: SourceFlow[] = await client
    .execute(
      `select source_id, label, cadence_human, metric_types, h3_resolution, attribution,
              docs_url, credentials
         from sources order by label`,
    )
    .then((result) =>
      (result.rows as unknown as Record<string, string>[]).map((row) => {
        let parsed: Record<string, string> = {};
        try {
          parsed = JSON.parse(row.credentials ?? "{}") as Record<string, string>;
        } catch {
          parsed = {};
        }
        const credentials = Object.entries(parsed).map(([name, state]) => ({
          name,
          present: state === "set" || state === "present" || state === "true",
        }));
        let metrics = 0;
        try {
          metrics = (JSON.parse(row.metric_types ?? "[]") as unknown[]).length;
        } catch {
          metrics = 0;
        }
        return {
          id: row.source_id,
          label: row.label,
          cadence: row.cadence_human,
          metrics,
          h3Resolution: Number(row.h3_resolution ?? 0),
          attribution: row.attribution,
          docsUrl: row.docs_url || null,
          credentials,
          // A source whose credentials are all missing cannot have run, which is the same
          // gap the stories page explains at length.
          blocked: credentials.length > 0 && credentials.every((entry) => !entry.present),
          // Which marts, if any, are built from this source. Empty is a real and currently
          // present state: the energy feed has been collected daily and no gold mart has
          // been built from it, so no page can show it. Reporting that beats letting a
          // registered source with a live credential imply it is on the dashboard.
          servedBy: martsById.get(row.source_id) ?? [],
        };
      }),
    );

  const statuses = await tableStatus(client);
  const marts = statuses
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
        domain: status.domain,
        rows: status.rows,
        present: status.present,
        lastValue: status.lastValue,
        daysBehind,
        budgetDays: MART_BUDGET_DAYS[status.name] ?? 30,
        blockedBy: BLOCKED_BY[status.name] ?? null,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));

  const model = await currentModel(client);
  const totalRuns = runs.reduce((sum, run) => sum + run.runs, 0);
  const totalOk = runs.reduce((sum, run) => sum + run.ok, 0);

  return {
    stages,
    sources,
    marts,
    model: model ? { version: model.version, scoredAt: model.scored_at, predictions: model.predictions } : null,
    totals: {
      tables: statuses.filter((status) => status.present).length,
      // Every table the registry knows about, including the ones that do not exist yet.
      // Reporting the count of dated marts here would quietly understate the gap.
      tablesTotal: statuses.length,
      rows: statuses.reduce((sum, status) => sum + (status.rows ?? 0), 0),
      runs: totalRuns,
      successRate: totalRuns > 0 ? totalOk / totalRuns : null,
    },
    generatedAt: new Date().toISOString(),
  };
}
