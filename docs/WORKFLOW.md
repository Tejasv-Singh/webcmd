# The Kosh multi-agent workflow

How eight subagents and five commands fit together to build and run the corpus.

## Why it's shaped this way

Three constraints drove every design decision here.

**Adding a source is the unit of work that repeats.** Kosh isn't one build — it's the
same pipeline run dozens of times against different sites over a year. So the workflow
is an assembly line with gates, not a project plan. The main command, `/new-source`,
is designed to be run again and again, getting more reliable each time as the briefs
and conventions accumulate.

**Subagents share no context.** Each spawn starts cold. This is the single most
important practical fact about building multi-agent workflows in Claude Code, and the
design consequence is that **the filesystem is the bus**: every agent reads its input
from a known path and writes its output to a known path before finishing. That makes
the pipeline resumable — if stage 4 fails, you re-run stage 4 rather than the whole
thing — and auditable, since every handoff leaves a durable artifact you can read
months later to understand why something was built the way it was.

**Separation of concerns is enforced by tool grants, not by discipline.** The most
elegant part of this setup: `extractor-author` has no browser and no network access,
which makes it *structurally impossible* for it to parse-on-fetch and violate the
raw/extract separation. Likewise `data-auditor` reports but doesn't fix, and
`canary-author` works from the brief without reading the implementation. Conventions
erode under deadline pressure; capability boundaries don't.

## The roster

| Agent | Role | Notable constraint |
|---|---|---|
| `compliance-reviewer` | ToS, robots, redistribution, SEBI check | Blocking gate — nothing starts without its verdict |
| `source-scout` | Read-only recon; finds the underlying JSON endpoint | Never mutates state on the target |
| `adapter-author` | Fetch and persist raw bytes | **Never parses** — no Observations |
| `extractor-author` | Raw → Observations, versioned | **No network, no browser** |
| `canary-author` | Assertions and drift thresholds | Works from brief, **never reads the adapter** |
| `data-auditor` | Independent accuracy measurement | **Reports, never fixes** |
| `entity-resolver` | ISIN graph, aliases, identity events | Defers rather than guessing |
| `pipeline-engineer` | Stores, scheduler, backfill, monitoring | Additive migrations only |
| `doc-intel` | PDFs → cited facts (Q2+) | Every fact carries a page citation |

## Workflow A — Add a source (`/new-source`)

The one that repeats. Note stage 3's parallelism and the two blocking gates.

```
  /new-source <slug> <url>
          │
    ┌─────▼──────────────────┐
    │ 1. compliance-reviewer │  GATE — STOP halts the pipeline entirely
    └─────┬──────────────────┘  → COMPLIANCE.md
          │
    ┌─────▼──────────┐
    │ 2. source-scout│  → BRIEF.md  (finds JSON endpoint if one exists)
    └─────┬──────────┘
          │
    ┌─────┴─────────────────┐
    ▼                       ▼          ← parallel, and independent by design
┌────────────────┐   ┌───────────────┐
│3a.adapter-     │   │3b.canary-     │  canary works from BRIEF only,
│   author       │   │   author      │  so it tests reality not code
└───────┬────────┘   └───────┬───────┘
        └──────────┬─────────┘
                   ▼
        ┌──────────────────────┐
        │ 4. extractor-author  │  offline, over blobs stage 3 captured
        └──────────┬───────────┘  → (entity-resolver if aliases unresolved)
                   ▼
        ┌──────────────────────┐
        │ 5. data-auditor      │  GATE — 95% on 30+ stratified facts
        └──────────┬───────────┘  findings route back to the owning agent
                   ▼
            schedule + monitor
```

The failure routing at stage 5 is the part worth internalizing: the auditor attributes
each finding to a cause — extractor bug, capture bug, or join error — and the
orchestrator sends it to the agent that owns that layer. The orchestrator never fixes
findings itself, because the moment it does, the audit stops being independent.

## Workflow B — Foundation (`/foundation`)

Runs once. `pipeline-engineer` builds the substrate, `entity-resolver` seeds the graph,
then a deliberately throwaway AMFI NAV adapter proves the loop end to end. The gate is
six specific checks, and the one that matters most is #4: re-running an extractor at a
bumped version must produce new rows *alongside* the old ones. If that doesn't work,
the entire architecture's payoff is absent and everything after it is built on sand.

## Workflow C — Daily health (`/health`)

Cheap, runs on `sonnet`, and it's the routine that catches the failure mode this whole
project is designed around. The rule that earns its keep: **zero rows on a day that
should have data is P0 until proven otherwise** — proven by checking the source, not
by assuming it was quiet.

## Workflow D — Backfill (`/backfill`)

Extraction backfill is the operation the raw/extract split exists to enable. Sample
first, diff old against new, investigate every difference, then audit whether accuracy
actually improved. Capture backfill is the other kind and is time-limited — sources
stop serving history, so gaps must be filled fast or they become permanent.

## Workflow E — Phase gates (`/gate`)

Quarterly, adversarial. Checks the things audits miss: whether collection is genuinely
unattended, whether canaries have ever actually fired, whether anything has been
silently abandoned. A gate that always passes isn't a gate.

## Model assignment

`opus` for the judgement-heavy roles — scouting an unfamiliar site, designing
extraction, auditing, entity resolution. `sonnet` for canary authoring, compliance
review and the daily health check, where the work is more procedural. Adjust once you
see where quality actually binds; the cost difference over a year of daily runs is
not trivial.

## Running it

```bash
# once
/foundation

# then, repeatedly, through Q1
/new-source nse-announcements https://www.nseindia.com/companies-listing/corporate-filings-announcements
/new-source bse-announcements https://www.bseindia.com/corporates/ann.html
/new-source amfi-nav https://www.amfiindia.com/spages/NAVAll.txt
/new-source sebi-circulars https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=7

# daily
/health

# quarterly
/gate q1
```

## Things that will go wrong

**A stage reports success but wrote nothing.** Always verify the artifact exists on
disk before advancing — a subagent's summary is not evidence.

**The brief is too vague and two agents build on guesswork.** Send it back for a
second pass; it's much cheaper than the rework.

**The orchestrator starts fixing audit findings itself** because it's faster than
another round-trip. This quietly destroys the independence that makes the gate
meaningful. Route findings to the owning agent.

**Someone lowers the audit threshold to get a pass.** The threshold is the only thing
standing between you and a corpus nobody can trust. If 95% is genuinely wrong for a
source, change it deliberately in this document with a written reason — don't drop it
in the moment.
