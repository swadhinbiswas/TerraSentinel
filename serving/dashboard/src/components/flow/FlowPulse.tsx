import { Link2 } from "lucide-react";
import type { FlowReport, StageRun } from "@/lib/flow";
import { relativeTime, STATE_META } from "./meta";

/**
 * The pipeline in one line, for the overview.
 *
 * The full rail lives on its own page. What belongs on the landing view is the thing a
 * reader needs before trusting anything else: is the data moving, and when did it last
 * move. Six dots and a link is enough for that, and it is the one question the rest of the
 * page depends on.
 *
 * Rendered on the server rather than as an island. The figures come from the same report
 * the pipeline page uses, so they cannot disagree, and a landing page that has to wait on
 * a client fetch to say whether the data is fresh is a landing page that briefly lies.
 */
export function FlowPulse({ report }: { report: FlowReport }) {
  const { stages, totals } = report;
  const now = Date.parse(report.generatedAt);
  const freshest = stages
    .map((stage) => stage.ageHours)
    .filter((age): age is number => age !== null)
    .reduce((best, age) => Math.min(best, age), Infinity);
  const needsAttention = stages.filter(
    (stage) => stage.state === "failed" || stage.state === "overdue",
  ).length;
  const unrecorded = stages.filter((stage) => stage.state === "unrecorded").length;

  return (
    <a
      href="/pipeline"
      className="card-interactive flex flex-wrap items-center gap-x-4 gap-y-2 rounded-[var(--radius-card)] border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 no-underline"
    >
      <span className="text-[11px] font-medium uppercase tracking-widest text-[var(--color-subtle)]">
        Pipeline
      </span>

      <span className="flex items-center gap-1.5" aria-hidden="true">
        {stages.map((stage) => (
          <Dot key={stage.stage.id} run={stage} />
        ))}
      </span>

      <span className="text-xs text-[var(--color-muted)]">
        <span className="sr-only">Stage status: </span>
        {needsAttention > 0
          ? `${needsAttention} stage${needsAttention === 1 ? "" : "s"} need attention`
          : unrecorded > 0
            ? `${unrecorded} with no run recorded`
            : `all ${stages.length} stages moving`}
      </span>

      <span className="text-xs text-[var(--color-subtle)]">
        {Number.isFinite(freshest) ? `freshest run ${relativeTime(new Date(now - freshest * 3_600_000).toISOString(), now)}` : "no runs yet"}
      </span>

      <span className="text-xs text-[var(--color-subtle)]">
        {totals.runs.toLocaleString()} runs · {totals.rows.toLocaleString()} rows served
      </span>

      <span className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-[var(--color-accent)]">
        <Link2 className="h-3 w-3" aria-hidden="true" />
        How the data gets here
      </span>
    </a>
  );
}

function Dot({ run }: { run: StageRun }) {
  const meta = STATE_META[run.state];
  return <span className={`h-1.5 w-4 rounded-full ${meta.dot}`} />;
}
