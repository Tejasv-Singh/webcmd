# Kosh — project context

Kosh is an Indian market intelligence corpus built on Webcmd. It collects public
market data, financial documents, and alternative-data observations from sites
that have no usable API, and stores them as an append-only time series.

**Read this file fully before doing any work. Every agent inherits it.**

## The one thing that matters

The asset is not the code. The asset is **the accumulated time series**, because it
cannot be bought retroactively. Nobody can sell you what a careers page looked like
last October. This has a direct consequence for every decision you make:

> **Never lose a capture. Never overwrite an observation. Never block collection
> on a refactor.** A day of missed collection is permanent data loss. A messy
> adapter that runs is worth more than a beautiful one that isn't deployed yet.

## Non-negotiable architecture

### 1. Observations are immutable and append-only

```
Observation
  entity_id       # NSE:INFY, AMFI:120503, ORG:zomato
  metric          # announcement.filed, nav.close, careers.openings.engineering
  value           # JSON scalar or object
  observed_at     # when the fact was true
  captured_at     # when we saw it
  source_url
  capture_id      # FK to the raw blob that produced this
  extractor_ver   # which parser version produced this
  confidence
```

Rows are **never** updated or deleted. Corrections are new rows with a later
`captured_at`. This is what lets us reconstruct what Kosh knew on any past date,
which is what makes backtesting honest.

### 2. Raw capture and extraction are separate steps

Adapters do exactly one job: **fetch and persist the raw bytes**, content-addressed
by hash. A separate extraction pass turns raw into Observations.

This is the highest-leverage rule in the codebase. It means improving an extractor
in month nine and re-running it over ten months of archived captures makes the
entire history better retroactively. Parse-on-fetch permanently forecloses that.

**Adapters must never emit Observations directly. Extractors must never touch
the network.** These are enforced by tool grants, not by discipline.

### 3. Entity resolution is keyed on ISIN

Infosys is `INFY` on NSE, `500209` on BSE, `INE009A01021` by ISIN, "Infosys Limited"
in a PDF header, "Infosys Ltd." in a footer. Resolve to ISIN wherever one exists.
Never join on a display name. Add unresolved aliases to the review queue rather
than guessing.

### 4. Silence is the dangerous failure

A scraper that crashes is a nuisance. One that quietly returns `[]` for three weeks
poisons the corpus. Every source ships with canary assertions and volume-drift
alerts *before* it goes into the schedule. An adapter returning zero rows on a
trading day is a P0 incident, not an empty result.

### 5. Credentials are Ring 3 only, local-first

Webcmd profiles are cookie jars: store sessions, never passwords. Two-factor stays
human-in-the-loop. Ring 3 (personal portfolio) data never leaves the local machine.
Rings 1 and 2 are the only things that may ever be hosted.

## The rings (build order)

- **Ring 1 — public market corpus.** NSE/BSE announcements and corporate actions,
  AMFI NAVs, shareholding patterns, fundamentals, SEBI/RBI circulars. No credentials.
- **Ring 2 — document + alt-data intelligence.** Annual reports, concall transcripts
  and investor presentations into cited facts; careers pages, pricing pages, app
  ranks, store counts.
- **Ring 3 — personal portfolio sync.** CAS from CAMS/KFintech, NSDL/CDSL, brokers.
  Last, local-only, opt-in.

Do not start a ring before the previous one collects reliably.

## Compliance rules that constrain the code

- **Never generate buy/sell/hold recommendations, price targets, or ratings**, in
  code, in output schemas, or in generated text. SEBI Research Analyst registration
  turns on taking payment for recommendations. Kosh states facts:
  "engineering openings went 40 → 65". It never says what to do about them.
- **Never resell or mirror raw exchange data.** Derived observations are the product.
- Respect rate limits and robots directives. Prefer a documented or observed JSON
  endpoint over DOM scraping; prefer DOM scraping over anything that looks like
  evading a control.
- A source is not touched until `sources/<slug>/COMPLIANCE.md` exists and says PROCEED.

## Repository layout

```
sources/<slug>/
  BRIEF.md          # source-scout: what exists, where, how to get it
  COMPLIANCE.md     # compliance-reviewer: verdict, must exist before adapter work
  adapter/          # webcmd adapter — fetch + persist raw only
  extractor/        # raw -> Observation, versioned, offline-testable
  canary/           # assertions + drift thresholds
  AUDIT.md          # data-auditor: accuracy findings per gate
core/
  store/            # observation store, raw blob store
  entities/         # ISIN alias graph, resolution
  scheduler/        # recurring collection, retries, backfill
  monitoring/       # freshness, drift, canary runner
docs/WORKFLOW.md    # how the agents fit together
```

## Handoffs between agents

Subagents do **not** share conversation context. The filesystem is the bus. Every
agent reads its input from a known path and writes its output to a known path
before finishing. If you are a subagent and your output is not on disk, your work
did not happen.

## Conventions

- Python 3.11+, `uv` for deps, `ruff` for lint, `pytest` for tests.
- Extractors are pure functions: `extract(raw_bytes, meta) -> list[Observation]`.
  They must be testable with no network and no browser.
- Every extractor bumps `extractor_ver` when its output changes for the same input.
- Timestamps are stored UTC, displayed IST. Market data is IST-native — be explicit.
- No secrets in the repo. `.env` is gitignored, `.env.example` is committed.
