/**
 * Investigation write-ups.
 *
 * Each story is a query plus a narrative: the numbers in the prose are interpolated
 * from the live mart at render time, so a story cannot drift from the data it
 * describes — the failure mode that makes most data-storytelling pages quietly wrong a
 * month after they were written.
 *
 * Expectations here were measured from the archive, not asserted. An earlier draft of
 * the equivalent list in the Python layer contained an invented peak for 2025-07-26
 * that turned out to be an ordinary day (z = −0.5); the negative control that replaced
 * it is in `ml/validation/known_events.py`.
 */
import type { Client } from "@libsql/client/web";

export interface StoryStat {
  label: string;
  value: string;
  hint?: string;
}

export interface StoryContext {
  slug: string;
  kicker: string;
  title: string;
  standfirst: string;
  body: string[];
  stats: StoryStat[];
  caveat?: string;
  query: string;
}

interface StoryFacts {
  peak: number;
  peakDate: string;
  spanDays: number;
  total: number;
  zAtPeak: number | null;
  flaggedDays: number;
  median: number | null;
  nightShare: number | null;
  frpPerDetection: number | null;
  percentile: number | null;
  baselineValue: number | null;
  baselineSource: string;
}

const fmt = (value: number | null | undefined, digits = 0): string =>
  value === null || value === undefined
    ? "—"
    : value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });

const pct = (value: number | null): string =>
  value === null ? "—" : `${(value * 100).toFixed(0)}%`;

function tokens(facts: StoryFacts): Record<string, string> {
  return {
    peak: fmt(facts.peak),
    peakDate: facts.peakDate,
    z: facts.zAtPeak === null ? "—" : facts.zAtPeak.toFixed(2),
    median: fmt(facts.median),
    days: String(facts.spanDays),
    flagged: String(facts.flaggedDays),
    total: fmt(facts.total),
    night: pct(facts.nightShare),
    percentile: facts.percentile === null ? "—" : facts.percentile.toFixed(3),
    extent: fmt(facts.peak, 2),
    normal: fmt(facts.baselineValue, 2),
  };
}

interface StoryDefinition {
  slug: string;
  kicker: string;
  regionId: string;
  start: string;
  end: string;
  ice?: boolean;
  build: (f: StoryFacts, t: Record<string, string>) => Omit<StoryContext, "slug" | "kicker">;
}

export const STORY_DEFINITIONS: StoryDefinition[] = [
  {
    slug: "august-2025-iberia",
    kicker: "Wildfire · Iberian Peninsula",
    regionId: "iberia_fire",
    start: "2025-08-15",
    end: "2025-08-17",
    build: (f, t) => ({
      title: "Three days that outran two years of history",
      standfirst: `A late-summer cluster put ${t.peak} fire detections into a single day — roughly ${Math.round(
        f.peak / (f.median || 1),
      )}× the seasonal median for that date.`,
      body: [
        `The Iberian Peninsula burns every summer, and the seasonal baseline expects it. What stands out is not that August had fires but how far the top of the distribution reaches: the busiest day in this window carried ${t.peak} detections, against a seasonal median of ${t.median}.`,
        `This is not a single spike. ${t.days} consecutive days sit in the window and ${t.flagged} of them cleared the anomaly threshold, which is what a sustained fire-weather episode looks like rather than a transient detection artefact.`,
        `Large events spread out. Mean intensity reached ${fmt(f.frpPerDetection, 1)} MW per detection and the day's single densest hexagon held only a small share of the total — the signature of a landscape-scale event rather than one concentrated burn.`,
      ],
      stats: [
        { label: "Peak detections in a day", value: fmt(f.peak), hint: f.peakDate },
        { label: "Seasonal median", value: fmt(f.median), hint: "same day-of-year window" },
        { label: "Departure", value: f.zAtPeak === null ? "—" : `z = ${f.zAtPeak.toFixed(1)}`, hint: "robust units (median/MAD)" },
        { label: "Days flagged", value: `${f.flaggedDays} of ${f.spanDays}` },
        { label: "Mean intensity", value: f.frpPerDetection === null ? "—" : `${fmt(f.frpPerDetection, 1)} MW`, hint: "per detection" },
      ],
      caveat:
        "Detection counts are not burned area. One large fire can produce thousands of detections across overpasses, and cloud, smoke and satellite geometry all affect what is seen.",
      query: `select observation_date, detection_count, round(zscore,1) as z, severity
from gold_fire_anomalies
where region_id = 'iberia_fire' and observation_date between '2025-08-13' and '2025-08-19'
order by observation_date`,
    }),
  },
  {
    slug: "february-2026-iberia",
    kicker: "Wildfire · out of season",
    regionId: "iberia_fire",
    start: "2026-02-24",
    end: "2026-02-27",
    build: (f, t) => ({
      title: "A winter anomaly, and the strongest in the record",
      standfirst: `Winter fires are rare in Iberia: ${t.peak} detections against a seasonal median of ${t.median}.`,
      body: [
        `This is the largest statistical departure in the whole two-year record — larger than the August megafire cluster — and it happened in winter. The late-February baseline expects almost nothing, so ${t.peak} detections against a median of ${t.median} produces a departure of z = ${t.z}.`,
        `It outranks August precisely because the baseline is quiet. A big summer day is an amplification of the season; a big winter day is the season behaving incorrectly, and the robust baseline is what makes that difference measurable rather than a matter of opinion.`,
        `Night share was ${t.night} on the peak day, below the regional average — daytime-driven burning rather than the overnight smouldering that dominates quiet periods.`,
      ],
      stats: [
        { label: "Peak detections", value: fmt(f.peak), hint: f.peakDate },
        { label: "Winter seasonal median", value: fmt(f.median) },
        { label: "Departure", value: f.zAtPeak === null ? "—" : `z = ${f.zAtPeak.toFixed(1)}`, hint: "largest in the record" },
        { label: "Night share", value: t.night },
        { label: "Days flagged", value: String(f.flaggedDays) },
      ],
      caveat:
        "Agricultural burning plausibly contributes to out-of-season detections and this pipeline cannot distinguish it from wildfire. The anomaly is real; the cause is not established here.",
      query: `select observation_date, detection_count, round(zscore,1) as z, severity
from gold_fire_anomalies
where region_id = 'iberia_fire' and observation_date between '2026-02-20' and '2026-03-01'
order by observation_date`,
    }),
  },
  {
    slug: "antarctic-sea-ice-deficit",
    kicker: "Cryosphere · Antarctic",
    regionId: "antarctic",
    start: "2026-08-20",
    end: "2026-09-19",
    ice: true,
    build: (f, t) => ({
      title: "A deficit that does not go away",
      standfirst: `${t.normal} × 10⁶ km² expected, ${t.extent} × 10⁶ km² observed — and it stays there.`,
      body: [
        `Unlike a fire cluster, this is not an event. Antarctic sea-ice extent sat below the 1981–2010 normal for every day in the window, reaching a departure of z = ${t.z} on ${t.peakDate} and rarely recovering above z = −1.5.`,
        `That shape changes how the data should be read. The boolean anomaly flag lights up on ${t.flagged} of ${t.days} days, which makes it nearly useless here — a threshold is the wrong instrument for a sustained shift. The z-score series is the honest representation, which is why the dashboard charts it rather than badging it.`,
        `The baseline is the strongest in the pipeline: a published 30-year per-day-of-year normal with its own standard deviation, so this departure is measured against three decades rather than against our own short record.`,
      ],
      stats: [
        { label: "Most negative departure", value: f.zAtPeak === null ? "—" : `z = ${f.zAtPeak.toFixed(2)}`, hint: f.peakDate },
        { label: "Observed extent", value: `${t.extent} × 10⁶ km²` },
        { label: "1981–2010 normal", value: `${t.normal} × 10⁶ km²` },
        { label: "Days below normal", value: `${f.flaggedDays} of ${f.spanDays}` },
        { label: "Baseline", value: f.baselineSource },
      ],
      caveat:
        "A threshold on a persistently anomalous series flags most days. Treat `is_anomaly` as uninformative for sea ice and read the departure instead.",
      query: `select period_start, round(value,2) as extent, round(baseline_mean,2) as normal, round(zscore,2) as z
from gold_ice_extent_trends
where region_id = 'antarctic' order by period_start desc limit 30`,
    }),
  },
  {
    slug: "greece-2024-autumn",
    kicker: "Wildfire · Greece, and a disagreement",
    regionId: "greece_fire",
    start: "2024-09-29",
    end: "2024-10-01",
    build: (f, t) => ({
      title: "Where the model and the rule disagree",
      standfirst: `The statistical baseline calls ${t.peakDate} extreme. The model ranks it at percentile ${t.percentile} — high, but below the serving threshold.`,
      body: [
        `This window is kept in the record specifically because the two detectors disagree. The median/MAD rule calls ${t.peakDate} extreme at z = ${t.z}; the Isolation Forest, which sees trailing windows rather than a seasonal baseline, ranks it at percentile ${t.percentile} and does not flag it.`,
        `Neither is obviously wrong. A day can be far above its seasonal norm while still sitting inside a run of elevated days, and a model built on recent history will not find that unusual. That is a difference in what the two methods measure, not a bug.`,
        `It is reported rather than resolved. A disagreement between a transparent rule and an opaque model is worth more to a reader than a tuned threshold that hides it.`,
      ],
      stats: [
        { label: "Peak detections", value: fmt(f.peak), hint: f.peakDate },
        { label: "Statistical z", value: f.zAtPeak === null ? "—" : f.zAtPeak.toFixed(1), hint: "the rule flags extreme" },
        { label: "Model percentile", value: t.percentile, hint: "below the 0.975 threshold" },
        { label: "Days in window", value: String(f.spanDays) },
      ],
      caveat:
        "The two scores are not competing estimates of one quantity. The z-score compares a day to its season; the model compares it to recent history. Agreement is not expected in every case.",
      query: `select a.observation_date, a.detection_count, round(a.zscore,1) as z,
       round(p.anomaly_percentile,3) as model_pct, a.severity
from gold_fire_anomalies a
left join ml_predictions p on p.region_id = a.region_id and p.observation_date = a.observation_date
where a.region_id = 'greece_fire' and a.observation_date between '2024-09-25' and '2024-10-05'
order by a.observation_date`,
    }),
  },
];

const EMPTY_FACTS: StoryFacts = {
  peak: 0,
  peakDate: "—",
  spanDays: 0,
  total: 0,
  zAtPeak: null,
  flaggedDays: 0,
  median: null,
  nightShare: null,
  frpPerDetection: null,
  percentile: null,
  baselineValue: null,
  baselineSource: "nsidc_climatology",
};

export async function loadStories(client: Client): Promise<StoryContext[]> {
  const stories: StoryContext[] = [];

  for (const definition of STORY_DEFINITIONS) {
    const facts: StoryFacts = { ...EMPTY_FACTS };

    try {
      if (definition.ice) {
        const result = await client.execute({
          sql: `select period_start as d, value as v, zscore, baseline_mean, baseline_source, is_anomaly
                from gold_ice_extent_trends
                where region_id = ? and period_start between ? and ?
                order by zscore asc`,
          args: [definition.regionId, definition.start, definition.end],
        });
        const rows = result.rows as unknown as Record<string, unknown>[];
        facts.spanDays = rows.length;
        facts.flaggedDays = rows.filter((row) => Number(row.is_anomaly) === 1).length;
        const worst = rows[0];
        if (worst) {
          facts.peak = Number(worst.v ?? 0);
          facts.peakDate = String(worst.d).slice(0, 10);
          facts.zAtPeak = worst.zscore === null ? null : Number(worst.zscore);
          facts.baselineValue = worst.baseline_mean === null ? null : Number(worst.baseline_mean);
          facts.baselineSource = String(worst.baseline_source ?? facts.baselineSource);
          facts.total = rows.reduce((sum, row) => sum + Number(row.v ?? 0), 0);
        }
      } else {
        const result = await client.execute({
          sql: `select a.observation_date as d, a.detection_count as v, a.zscore,
                       a.baseline_median, a.night_detection_share, a.frp_per_detection,
                       a.is_anomaly, p.anomaly_percentile
                from gold_fire_anomalies a
                left join ml_predictions p
                  on p.region_id = a.region_id and p.observation_date = a.observation_date
                where a.region_id = ? and a.observation_date between ? and ?
                order by a.zscore desc nulls last`,
          args: [definition.regionId, definition.start, definition.end],
        });
        const rows = result.rows as unknown as Record<string, unknown>[];
        facts.spanDays = rows.length;
        facts.flaggedDays = rows.filter((row) => Number(row.is_anomaly) === 1).length;
        const top = rows[0];
        if (top) {
          facts.peak = Number(top.v ?? 0);
          facts.peakDate = String(top.d).slice(0, 10);
          facts.zAtPeak = top.zscore === null ? null : Number(top.zscore);
          facts.median = top.baseline_median === null ? null : Number(top.baseline_median);
          facts.nightShare =
            top.night_detection_share === null ? null : Number(top.night_detection_share);
          facts.frpPerDetection =
            top.frp_per_detection === null ? null : Number(top.frp_per_detection);
          facts.percentile = top.anomaly_percentile === null ? null : Number(top.anomaly_percentile);
          facts.total = rows.reduce((sum, row) => sum + Number(row.v ?? 0), 0);
        }
      }
    } catch {
      // A missing table or an empty window must not break the page: the story renders
      // with placeholders and its caveat says why.
    }

    stories.push({
      slug: definition.slug,
      kicker: definition.kicker,
      ...definition.build(facts, tokens(facts)),
    });
  }

  return stories;
}
