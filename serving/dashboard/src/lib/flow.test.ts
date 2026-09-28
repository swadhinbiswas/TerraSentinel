import { describe, expect, it } from "vitest";
import { flowReport, STAGES, type StageId } from "./flow";

/**
 * The pipeline's stage-state derivation.
 *
 * These five states are the whole point of the page: they decide whether a reader believes
 * the pipeline is healthy. The thresholds encode business rules — a fire feed is late after
 * a few days, a weekly retrain is not late for a week — so they are worth pinning rather
 * than re-deriving by eye whenever a card is restyled.
 *
 * The client is a stub. What matters here is the mapping from rows to state, not the SQL.
 */

type Row = Record<string, unknown>;

function clientReturning(rows: Row[], sources: Row[] = [], tables: Row[] = []) {
  return {
    execute: async (input: unknown) => {
      const sql = String(typeof input === "object" && input !== null && "sql" in input ? (input as { sql: string }).sql : input);
      if (sql.includes("from pipeline_runs") && sql.includes("row_number")) {
        return { rows };
      }
      if (sql.includes("from sources")) {
        return { rows: sources };
      }
      return { rows: tables };
    },
  } as never;
}

const HOUR = 3_600_000;

function runFor(workflow: string, overrides: Partial<Row> = {}): Row {
  return {
    workflow,
    runs: 10,
    ok: 10,
    last_run: new Date(Date.now() - HOUR).toISOString(),
    last_ok: new Date(Date.now() - HOUR).toISOString(),
    rows_total: 1000,
    failed_units: 0,
    last_status: "success",
    duration_s: 12,
    last_rows: 500,
    ...overrides,
  };
}

function stateOf(report: Awaited<ReturnType<typeof flowReport>>, id: StageId) {
  const found = report.stages.find((stage) => stage.stage.id === id);
  if (!found) throw new Error(`no stage ${id}`);
  return found;
}

describe("STAGES", () => {
  it("declares a workflow for every stage that can have one", () => {
    // `serve` is the only stage with no run of its own; the dashboard being up is the
    // evidence for it, and a workflow name there would be an invention.
    for (const stage of STAGES) {
      if (stage.id === "serve") expect(stage.workflow).toBeNull();
      else expect(stage.workflow).toBeTruthy();
    }
  });

  it("gives Train and Score the same workflow, because they are one job", () => {
    const train = STAGES.find((s) => s.id === "train");
    const score = STAGES.find((s) => s.id === "score");
    expect(train?.workflow).toBe("train_model");
    expect(score?.workflow).toBe("train_model");
  });

  it("orders the stages the way the data moves", () => {
    expect(STAGES.map((s) => s.id)).toEqual([
      "collect",
      "store",
      "transform",
      "train",
      "score",
      "serve",
    ]);
  });
});

describe("stage state", () => {
  it("is live when the last run succeeded and is inside its budget", async () => {
    const report = await flowReport(clientReturning([runFor("collect_data")]));
    expect(stateOf(report, "collect").state).toBe("live");
  });

  it("is overdue when the last run is older than the stage's budget", async () => {
    // The collect budget is 8 hours; 3 days is well past it.
    const old = new Date(Date.now() - 72 * HOUR).toISOString();
    const report = await flowReport(clientReturning([runFor("collect_data", { last_run: old })]));
    const collect = stateOf(report, "collect");
    expect(collect.state).toBe("overdue");
    expect(collect.note).toMatch(/budget/i);
  });

  it("is failed when the most recent run did not succeed", async () => {
    const report = await flowReport(
      clientReturning([runFor("collect_data", { last_status: "failed" })]),
    );
    const collect = stateOf(report, "collect");
    expect(collect.state).toBe("failed");
    expect(collect.note).toMatch(/failed/i);
  });

  it("prefers `failed` over `overdue` when a stage is both", async () => {
    // A stage that failed and has not run since is broken, not merely late, and the copy
    // has to say which.
    const old = new Date(Date.now() - 72 * HOUR).toISOString();
    const report = await flowReport(
      clientReturning([runFor("collect_data", { last_run: old, last_status: "failed" })]),
    );
    expect(stateOf(report, "collect").state).toBe("failed");
  });

  it("is unrecorded when a workflow that should write runs has written none", async () => {
    const report = await flowReport(clientReturning([runFor("collect_data")]));
    const train = stateOf(report, "train");
    expect(train.state).toBe("unrecorded");
    expect(train.note).toMatch(/records a run on every execution/i);
    // An unrecorded stage must not present a duration or a row count it does not have.
    expect(train.durationS).toBeNull();
    expect(train.rows).toBeNull();
    expect(train.lastRun).toBeNull();
  });

  it("never reports a run the database does not have", async () => {
    const report = await flowReport(clientReturning([]));
    for (const stage of report.stages) {
      if (stage.stage.workflow === null) continue;
      expect(stage.runs).toBe(0);
      expect(stage.state).toBe("unrecorded");
    }
  });

  it("treats the serving stage as external, because there is no run to read", async () => {
    const report = await flowReport(clientReturning([]));
    expect(stateOf(report, "serve").state).toBe("external");
  });

  it("reports the same run under both stages that share a workflow", async () => {
    // Reporting it twice is honest. Splitting the count or inventing a second run is not.
    const report = await flowReport(
      clientReturning([runFor("train_model", { duration_s: 90, last_rows: 1200 })]),
    );
    const train = stateOf(report, "train");
    const score = stateOf(report, "score");
    expect(train.lastRun).toBe(score.lastRun);
    expect(train.runs).toBe(score.runs);
    expect(train.durationS).toBe(score.durationS);
  });

  it("computes age from the last run, not the last success", async () => {
    const report = await flowReport(
      clientReturning([
        runFor("transform", {
          last_run: new Date(Date.now() - 2 * HOUR).toISOString(),
          last_ok: new Date(Date.now() - 40 * HOUR).toISOString(),
        }),
      ]),
    );
    const transform = stateOf(report, "transform");
    expect(transform.ageHours).toBeGreaterThan(1.5);
    expect(transform.ageHours).toBeLessThan(2.5);
  });

  it("computes a success rate only when there is a denominator", async () => {
    const report = await flowReport(clientReturning([runFor("collect_data", { runs: 8, ok: 6 })]));
    expect(stateOf(report, "collect").successRate).toBe(0.75);
    expect(stateOf(report, "transform").successRate).toBeNull();
  });
});

describe("source credential state", () => {
  const source = (credentials: string, extra: Partial<Row> = {}): Row => ({
    source_id: "x",
    label: "X",
    cadence_human: "daily",
    metric_types: '["a","b"]',
    h3_resolution: "7",
    attribution: "X data",
    docs_url: "https://example.test",
    credentials,
    ...extra,
  });

  it("marks a source blocked only when it declares credentials and none are set", async () => {
    const blocked = await flowReport(
      clientReturning([], [source('{"K":"missing","J":"missing"}')]),
    );
    expect(blocked.sources[0].blocked).toBe(true);
    expect(blocked.sources[0].credentials).toEqual([
      { name: "K", present: false },
      { name: "J", present: false },
    ]);

    const ok = await flowReport(clientReturning([], [source('{"K":"set"}')]));
    expect(ok.sources[0].blocked).toBe(false);
  });

  it("does not call a keyless source blocked just because it has no credentials", async () => {
    // NOAA/NSIDC is public and keyless. Reporting it blocked would be a false alarm.
    const report = await flowReport(clientReturning([], [source("{}")]));
    expect(report.sources[0].blocked).toBe(false);
  });

  it("treats a present, set and true as present", async () => {
    const report = await flowReport(
      clientReturning([], [source('{"A":"set","B":"present","C":"true"}')]),
    );
    expect(report.sources[0].credentials.every((c) => c.present)).toBe(true);
  });

  it("survives unparseable credential and metric JSON", async () => {
    const report = await flowReport(
      clientReturning([], [source("not json", { metric_types: "not json" })]),
    );
    expect(report.sources[0].credentials).toEqual([]);
    expect(report.sources[0].metrics).toBe(0);
    expect(report.sources[0].blocked).toBe(false);
  });
});

describe("totals", () => {
  it("is null rather than zero when nothing has run", async () => {
    // A 0% success rate would read as "everything failed", which is a different claim.
    const report = await flowReport(clientReturning([]));
    expect(report.totals.runs).toBe(0);
    expect(report.totals.successRate).toBeNull();
  });

  it("aggregates across workflows", async () => {
    const report = await flowReport(
      clientReturning([runFor("collect_data", { runs: 30, ok: 29 }), runFor("transform", { runs: 10, ok: 10 })]),
    );
    expect(report.totals.runs).toBe(40);
    expect(report.totals.successRate).toBeCloseTo(39 / 40);
  });
});
