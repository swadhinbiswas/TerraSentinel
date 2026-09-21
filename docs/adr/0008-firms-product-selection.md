# ADR 0008 — FIRMS archive selection and quota pacing

**Status:** accepted · implemented in `collectors/firms_collector.py` and
`ops/resilience.py`

## Context

NASA FIRMS publishes each instrument twice: a near-real-time product (`*_NRT`) and a
standard-processing product (`*_SP`). The original design picked between them on a
single question — "is this run a backfill?" — using `*_SP` for backfills and `*_NRT`
for live runs. Both halves of that were wrong, and only real data showed it.

Measured against the live API on 2026-09-21 (Iberia, 5-day windows):

| window start | VIIRS_SNPP_SP | MODIS_SP | VIIRS_SNPP_NRT |
|---|---|---|---|
| 2026-05-01 | 0 | 13 | — |
| 2026-06-01 | 249 | 74 | **0** |
| 2026-06-15 | 236 | 41 | — |
| 2026-07-01 | **0** | **0** | 2267 |
| 2026-08-01 | 0 | 0 | 368 |
| 2026-09-10 | 0 | 0 | 413 |

Two facts follow, neither of them documented as plainly as they need to be:

* **SP is published roughly three months behind real time.** A backfill using SP
  returns *silently empty* results for the most recent quarter — the period that
  matters most for a monitoring pipeline. It looks like "no fires", not like an error.
* **NRT is retained for roughly three months.** Beyond that the NRT archive is gone,
  so SP is the only remaining source for older history.

The two windows therefore overlap, and they move.

Separately, quota behaviour did not match the documentation. FIRMS documents 5000
transactions per 10 minutes, noting that one request may cost several. In practice:

| run | pace | outcome |
|---|---|---|
| 1 | ~1.5 req/s sustained | ~890 requests, one 400 near the end |
| 2 | immediate, after run 1 | throttled almost at once |
| 3 | ~1.8 req/s | throttled after ~180 requests |
| 4 | ~1.3 req/s | throttled after ~90 requests |

Throttling is reported as `HTTP 400 "Invalid MAP_KEY"` — not 429 — and the same URLs
returned 200 when probed individually minutes later. So it is neither a bad key nor a
bad request, and a naive "4xx is permanent" rule would have discarded the data.

## Decision

**Product selection is driven by window age, not by run type.** With
`SP_MIN_AGE_DAYS = 100` and `NRT_MAX_AGE_DAYS = 112`, a window is served by SP where
the archive exists, by NRT where it does not, and by both when it sits in the overlap
— SP preferred, NRT used only if SP comes back with nothing. That closes the moving
boundary without ever treating "preferred archive is empty" as "no fires".

**Quota is enforced by a persisted sliding-window budget**, `TransactionBudget`, at
1500 transactions per 10 minutes rather than the documented 5000. The documented
figure is treated as an upper bound to stay well under, not a target.

**HTTP 400 is retryable for this source.** `FirmsCollector.transient_client_errors`
lists it, because FIRMS uses it for throttling. A genuinely bad credential still fails
after the retry budget and trips the breaker.

**The breaker counts attempts, so its threshold scales with the retry budget.** A
threshold of 5 with 4 attempts per request means ~1.25 failed requests ends the run;
FIRMS uses 12. This is now stated in `CircuitBreaker.failure_threshold` so the next
source does not rediscover it.

## Consequences

- A two-year, two-region, three-instrument backfill is ~900 requests and takes around
  30 minutes at the safe pace. Acceptable for a one-time job; the live collectors make
  3 requests per run.
- **The budget state is persisted** (`data/state/firms_quota.json`). The quota window
  lives on the server, so a second run minutes after the first inherits a nearly full
  window; without persistence it would start with an empty local budget, overspend,
  and be throttled. This is exactly what happened in runs 2-4.
- **A penalty may outlast the window.** Run 4 was throttled sooner than run 1 at a
  lower rate, which suggests a graduated penalty after repeated violations. The
  practical consequence for an operator is: after a throttled run, wait before
  retrying rather than immediately re-running with the same settings.
- Region failures no longer discard a long fetch. `run()` flushes deferred uploads
  even when some regions failed, because losing ten minutes of collected data to one
  flaky window is worse than landing partial data that the run record already
  describes as partial.
- Gaps are possible in principle where neither archive covers a window. The collector
  counts those windows and logs them with the re-run instruction rather than letting
  them pass as fire-free days. With the measured constants they should not occur.

## Alternatives rejected

- **`*_SP` for all backfills, `*_NRT` for live** — the original design; silently
  produces empty data for the most recent quarter and is unusable immediately after a
  backfill.
- **Always fetching both archives and deduplicating** — doubles quota consumption,
  which is the binding constraint, and SP/NRT differ enough (different confidence
  encoding, slightly different coordinates) that exact-key dedupe would not reliably
  collapse them. Double-counted fires would corrupt the anomaly signal.
- **Fixed inter-request sleep** — cannot express "5000 per 10 minutes where a request
  may cost 5". Looks polite while spending the entire budget in one run.
- **Checking `/api/data_availability/` before each window** — one extra request per
  window to answer a question the window age already answers, at a time when requests
  are the scarce resource.
- **Treating the documented 5000 as the limit** — measurably wrong; run 3 was
  throttled having spent well under half of it.
