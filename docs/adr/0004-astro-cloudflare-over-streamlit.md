# ADR 0004 — Astro + Cloudflare Pages over Streamlit (or an HF Space)

**Status:** accepted · implemented in Phase 6

## Context

The dashboard has to show a map of anomalies, per-region time series, and a "last updated"
indicator — publicly, cheaply, and without looking like a default data-science demo. The
obvious zero-effort option is Streamlit on Hugging Face Spaces, so the choice needs arguing.

## Decision

**Astro** with React islands for the two genuinely interactive pieces (`maplibre-gl` map,
`recharts` trend chart), **shadcn/ui + Tailwind** for the component system, deployed to
**Cloudflare Pages**. Reads go through Astro API routes running as Pages Functions, backed by
Turso. Optional caching via the Cache API.

## Why

- **Ship static, hydrate islands.** The page is mostly HTML with two or three interactive
  widgets. Astro's default of zero client JS makes that the cheap path; a Streamlit app
  re-renders the world on every interaction.
- **`workerd` can reach Turso directly.** libSQL's HTTP API works from the edge runtime, so
  there is no separate backend service between the dashboard and the database (ADR 0002).
- **CPU budget forces a good architecture.** Pages Functions have a short per-request CPU
  limit on the free tier. That constraint is why scoring happens in GitHub Actions and the
  routes only do a SQL read — the limit is a feature, not friction.
- **Design control.** `shadcn/ui` is real components you own, not a themed widget set. For a
  public-facing artifact, the difference between "a dashboard" and "a Streamlit app" is most
  of the impression it makes.
- **Cloudflare Pages deploys from git** with unlimited requests on the free tier, which pairs
  naturally with edge-replicated reads.

## Consequences

- Two toolchains in the repo (Python + Node). Contained to `serving/dashboard/`, excluded from
  the Python lint config, and the deploy is a separate workflow.
- Interactive pieces are written as islands, so state shared between the map and the chart has
  to be lifted deliberately rather than by a framework-wide re-run.
- Turso credentials live in Pages environment variables and are read **server-side only**; the
  browser only ever talks to `/api/*`. A `lib/turso.ts` singleton is the single place that
  knows the connection, and it must never be imported into a client component.

## Alternatives rejected

- **Streamlit on HF Spaces** — fastest to a demo, and the wrong long-term shape: a Python
  server on the request path, limited layout control, and no path to edge caching.
- **Next.js** — would do the job, but ships a large client runtime by default for a page that
  needs almost none. Astro's islands model fits this content shape better.
- **Observable / evidence.dev / a static site generator with no API routes** — the dashboard
  needs parameterised queries (by type, region, window) and a health endpoint, so a pure
  static build would force every query to be pre-rendered.
- **A FastAPI backend plus a separate frontend** — that is the architecture this design
  deliberately avoids: a live Python service on the request path, for reads that are already
  precomputed into a database.
