import {
  Brain,
  Database,
  Globe,
  RefreshCw,
  Satellite,
  Target,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ComponentType } from "react";
import type { FlowReport, StageId, StageRun } from "@/lib/flow";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { compactNumber, duration, relativeTime, STATE_META, wireClass } from "./meta";

/**
 * The pipeline, live.
 *
 * The page arrives with a report already rendered from the server, so the first paint is
 * a real answer rather than a skeleton. After that this polls `/api/flow` on an interval
 * and swaps the report in, which means a reader who leaves the tab open sees a stage fall
 * overdue without reloading.
 *
 * Two rules the component keeps:
 *
 *   * **A failed poll changes nothing.** The previous report stays on screen and the
 *     indicator says the refresh failed. Blanking a working pipeline view because one
 *     request timed out trades a small inaccuracy for a large one.
 *   * **A stage with no recorded run is drawn as a gap, not as fine.** Every workflow
 *     records a run on completion, so an empty history is information rather than absence.
 */

const POLL_MS = 30_000;

const ICONS: Record<StageId, ComponentType<{ className?: string }>> = {
  collect: Satellite,
  store: Database,
  transform: WorkflowIcon,
  train: Brain,
  score: Target,
  serve: Globe,
};

export default function FlowRail({ initial }: { initial: FlowReport }) {
  const [report, setReport] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const inFlight = useRef(false);

  useEffect(() => {
    // Relative times are recomputed on a second tick. A stage only reports at minute
    // resolution, so anything finer would be layout work with no visible result.
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const response = await fetch("/api/flow", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setReport((await response.json()) as FlowReport);
      setNow(Date.now());
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      inFlight.current = false;
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const poll = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(poll);
  }, [refresh]);

  // A tab left in the background should not accumulate a backlog of polls, and returning
  // to a stale view is worse than returning to a fresh one.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [refresh]);

  const problems = report.stages.filter((run) => run.state === "failed" || run.state === "overdue");
  const unrecorded = report.stages.filter((run) => run.state === "unrecorded");

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="relative flex h-2 w-2" aria-hidden="true">
          <span className="pulse-ring absolute inline-flex h-full w-full rounded-full bg-[var(--color-i3)]" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-[var(--color-i3)]" />
        </span>
        <span className="text-xs text-[var(--color-muted)]">
          Live · refreshed {relativeTime(report.generatedAt, now)}
        </span>
        <button
          type="button"
          onClick={() => void refresh()}
          className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-muted)] transition-colors hover:border-[var(--color-border-strong)] hover:text-[var(--color-foreground)]"
        >
          <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} aria-hidden="true" />
          Refresh
        </button>
        {error && (
          <span className="text-xs text-[var(--color-i5)]">
            Refresh failed ({error}) — still showing the last good report
          </span>
        )}
        {problems.length > 0 && (
          <span className="text-xs text-[var(--color-i4)]">
            {problems.length} stage{problems.length === 1 ? "" : "s"} need attention
          </span>
        )}
        {unrecorded.length > 0 && (
          <span className="text-xs text-[var(--color-subtle)]">
            {unrecorded.length} stage{unrecorded.length === 1 ? "" : "s"} with no run recorded
          </span>
        )}
      </div>

      <ol className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
        {report.stages.map((run, index) => (
          <StageCard key={run.stage.id} run={run} index={index} last={index === report.stages.length - 1} now={now} />
        ))}
      </ol>
    </div>
  );
}

function StageCard({ run, index, last, now }: { run: StageRun; index: number; last: boolean; now: number }) {
  const meta = STATE_META[run.state];
  const Icon = ICONS[run.stage.id];
  const needsAttention = run.state === "failed" || run.state === "overdue";

  return (
    <li className="relative">
      <div
        className={`card-interactive flex h-full flex-col rounded-[var(--radius-card)] border bg-[var(--color-surface)] p-3.5 ${meta.border}`}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-[var(--color-border)] bg-[var(--color-surface-raised)]">
              <Icon className="h-3.5 w-3.5 text-[var(--color-muted)]" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <p className="text-sm font-semibold leading-tight tracking-tight">{run.stage.label}</p>
              <p className="truncate text-[11px] leading-tight text-[var(--color-subtle)]">
                {String(index + 1).padStart(2, "0")} · {run.stage.where}
              </p>
            </div>
          </div>
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  className={`mt-1 inline-flex h-2.5 w-2.5 shrink-0 rounded-full ${meta.dot} ${needsAttention ? "animate-pulse" : ""}`}
                />
              </TooltipTrigger>
              <TooltipContent side="top">{meta.label}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>

        <p className="mt-2.5 text-xs leading-relaxed text-[var(--color-muted)]">{run.stage.what}</p>

        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 border-t border-[var(--color-border)] pt-2.5 text-[11px]">
          <div>
            <dt className="text-[var(--color-subtle)]">State</dt>
            <dd className={`font-medium ${meta.text}`}>{meta.label}</dd>
          </div>
          <div>
            <dt className="text-[var(--color-subtle)]">Last run</dt>
            <dd className="tabular-nums text-[var(--color-foreground)]">
              {relativeTime(run.lastRun, now)}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--color-subtle)]">Rows</dt>
            <dd className="tabular-nums text-[var(--color-foreground)]">
              {run.rows === null ? "—" : compactNumber(run.rows)}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--color-subtle)]">Duration</dt>
            <dd className="tabular-nums text-[var(--color-foreground)]">{duration(run.durationS)}</dd>
          </div>
        </dl>

        {run.stage.cron && (
          <p className="mt-2 truncate font-mono text-[10px] text-[var(--color-subtle)]">
            {run.stage.workflow ?? "schedule"} · {run.stage.cron}
          </p>
        )}

        {run.successRate !== null && (
          <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--color-subtle)]">
            {run.runs} run{run.runs === 1 ? "" : "s"} · {Math.round(run.successRate * 100)}% succeeded
            {run.failedUnits
              ? ` · ${run.failedUnits} failed unit${run.failedUnits === 1 ? "" : "s"}`
              : ""}
          </p>
        )}

        {run.note && (
          <p
            className={`mt-2.5 rounded-md border px-2 py-1.5 text-[11px] leading-relaxed text-[var(--color-muted)] ${meta.surface} ${meta.border}`}
          >
            {run.note}
          </p>
        )}
      </div>

      {/* The wire leaves the bottom of each card in the stacked layout and the right in the
          single-row one, so the reading order is the same either way. Two elements rather
          than one with a breakpoint, because the animated sweep has to run along the axis
          the wire actually follows. */}
      {!last && (
        <>
          <span
            aria-hidden="true"
            className={`absolute -bottom-3 left-5 h-3 w-px 2xl:hidden ${wireClass(run.state, true)}`}
          />
          <span
            aria-hidden="true"
            className={`absolute -right-3 top-1/2 hidden h-px w-3 -translate-y-1/2 2xl:block ${wireClass(
              run.state,
              false,
            )}`}
          />
        </>
      )}
    </li>
  );
}
