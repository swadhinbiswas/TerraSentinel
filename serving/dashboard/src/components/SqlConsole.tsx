import { useCallback, useEffect, useMemo, useState } from "react";

/**
 * A guarded read-only SQL console.
 *
 * The guard is server-side (`src/lib/sql-guard.ts`) because a browser-side check protects
 * nothing. What matters here is that the UI tells the truth about the risk: the banner
 * reports the token's scope, so nobody assumes a safety the route does not have.
 */
interface TableInfo {
  name: string;
  title: string;
  present: boolean;
  rows: number | null;
  columns: string[];
}

interface QueryResponse {
  columns?: string[];
  rows?: Record<string, unknown>[];
  truncated?: boolean;
  elapsedMs?: number;
  error?: string;
}

const EXAMPLES: { label: string; sql: string }[] = [
  {
    label: "Worst fire days",
    sql: "select region_id, observation_date, detection_count, round(zscore,1) as z, severity\nfrom gold_fire_anomalies\norder by zscore desc limit 15",
  },
  {
    label: "February anomalies",
    sql: "select region_id, observation_date, detection_count, round(zscore,1) as z\nfrom gold_fire_anomalies\nwhere strftime('%m', observation_date) = '02' and is_anomaly = 1\norder by zscore desc",
  },
  {
    label: "Model vs rule",
    sql: "select a.observation_date, a.detection_count, round(a.zscore,1) as rule_z,\n       round(p.anomaly_percentile,3) as model_pct, a.severity\nfrom gold_fire_anomalies a\njoin ml_predictions p\n  on p.region_id = a.region_id and p.observation_date = a.observation_date\nwhere a.is_anomaly = 1 order by model_pct limit 15",
  },
  {
    label: "Cell concentration",
    sql: "select region_id, count(*) as cells, max(value) as peak_cells,\n       round(avg(value),2) as mean_cells\nfrom gold_h3_fire group by region_id",
  },
  {
    label: "Sea-ice departures",
    sql: "select region_id, period_start, round(value,2) as extent, round(baseline_mean,2) as normal, round(zscore,2) as z\nfrom gold_ice_extent_trends order by zscore limit 12",
  },
  {
    label: "Pipeline history",
    sql: "select workflow, status, started_at, rows_written from pipeline_runs order by started_at desc limit 10",
  },
];

export default function SqlConsole() {
  const [sql, setSql] = useState(EXAMPLES[0].sql);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<{ tables: TableInfo[]; tokenScope: string; maxRows: number } | null>(null);

  useEffect(() => {
    fetch("/api/query")
      .then((response) => response.json() as Promise<typeof info>)
      .then((data) => setInfo(data))
      .catch(() => setInfo(null));
  }, []);

  const run = useCallback(async () => {
    setBusy(true);
    try {
      const response = await fetch("/api/query", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ sql }),
      });
      setResult((await response.json()) as QueryResponse);
    } catch (error) {
      setResult({ error: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(false);
    }
  }, [sql]);

  const writable = info?.tokenScope === "rw";
  const readableTables = useMemo(
    () => (info?.tables ?? []).filter((table) => table.present),
    [info],
  );

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_260px]">
      <div>
        {writable && (
          <p className="mb-3 rounded border border-[var(--color-extreme)]/60 bg-[color-mix(in_oklab,var(--color-extreme)_12%,transparent)] px-3 py-2 text-xs">
            <strong>Read-write token.</strong> This route is protected by a statement guard
            (SELECT-only, single statement, row cap) because the database credential in use can
            also write. A read-only Turso token is the stronger control for this route.
          </p>
        )}
        <textarea
          value={sql}
          onChange={(event) => setSql(event.target.value)}
          spellCheck={false}
          rows={9}
          className="w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] p-3 font-mono text-xs leading-relaxed outline-none focus:border-[var(--color-accent)]"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={run}
            disabled={busy}
            className="rounded bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-black disabled:opacity-50"
          >
            {busy ? "running…" : "Run query"}
          </button>
          {info && (
            <span className="text-xs text-[var(--color-muted)]">
              SELECT only · one statement · max {info.maxRows} rows
            </span>
          )}
          {result?.elapsedMs !== undefined && (
            <span className="ml-auto text-xs text-[var(--color-muted)]">
              {result.rows?.length ?? 0} row(s) in {result.elapsedMs} ms
              {result.truncated ? " · truncated at the row cap" : ""}
            </span>
          )}
        </div>

        {result?.error && (
          <p className="mt-3 rounded border border-[var(--color-extreme)]/60 px-3 py-2 font-mono text-xs text-[var(--color-extreme)]">
            {result.error}
          </p>
        )}

        {result?.columns && result.rows && (
          <div className="mt-3 max-h-[420px] overflow-auto rounded border border-[var(--color-border)]">
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 bg-[var(--color-surface)]">
                <tr>
                  {result.columns.map((column) => (
                    <th key={column} className="border-b border-[var(--color-border)] px-2 py-1.5 text-left font-medium text-[var(--color-muted)]">
                      {column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((row, index) => (
                  <tr key={index} className="odd:bg-[color-mix(in_oklab,var(--color-surface)_60%,transparent)]">
                    {result.columns!.map((column) => (
                      <td key={column} className="border-b border-[var(--color-border)]/50 px-2 py-1 font-mono tabular-nums">
                        {row[column] === null ? <span className="text-[var(--color-muted)]">null</span> : String(row[column])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <aside className="space-y-3">
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">Examples</p>
          <ul className="space-y-1">
            {EXAMPLES.map((example) => (
              <li>
                <button
                  type="button"
                  onClick={() => setSql(example.sql)}
                  className="w-full rounded border border-[var(--color-border)] px-2 py-1 text-left text-xs text-[var(--color-muted)] hover:text-[var(--color-foreground)]"
                >
                  {example.label}
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">
            Queryable tables
          </p>
          <ul className="space-y-0.5 text-xs">
            {readableTables.map((table) => (
              <li key={table.name} className="flex items-baseline justify-between gap-2">
                <button
                  type="button"
                  onClick={() => setSql(`select * from ${table.name} limit 20`)}
                  className="font-mono text-[var(--color-accent)] underline decoration-dotted"
                >
                  {table.name}
                </button>
                <span className="tabular-nums text-[var(--color-muted)]">
                  {table.rows?.toLocaleString() ?? "—"}
                </span>
              </li>
            ))}
            {readableTables.length === 0 && <li className="text-[var(--color-muted)]">none available</li>}
          </ul>
        </div>
      </aside>
    </div>
  );
}
