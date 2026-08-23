# Kosh

An Indian market intelligence corpus: public market data, financial documents and
alternative-data observations, collected from sites with no usable API and stored as
an **append-only time series**.

The asset is not the code. The asset is the accumulated history, because it cannot be
bought retroactively — nobody can sell you what a careers page looked like last
October. Read [`CLAUDE.md`](CLAUDE.md) before changing anything; it is the constitution
this codebase implements.

## Quick start

Nothing to install for the SQLite path — the store, the CLI and the whole test suite
run on a stock Python 3.11+.

```bash
python -m core.cli verify-foundation     # the six foundation checks, executed
python -m pytest -q                      # 98 tests, no network, no database server
```

Then, for real collection:

```bash
cp .env.example .env                     # set KOSH_USER_AGENT to something with a contact
docker compose up -d                     # Postgres on 5433
export KOSH_DATABASE_URL=postgresql://kosh:kosh@localhost:5433/kosh
kosh migrate
kosh sources register --slug amfi-nav-probe --enable \
    --url https://www.amfiindia.com/spages/NAVAll.txt
kosh run --source amfi-nav-probe --date today
kosh health
```

## What is here

```
core/
  store/        append-only observation store + content-addressed blob store
  entities/     ISIN-keyed identity graph, aliases, identity events, review queue
  scheduler/    source registry, calendars, run execution, gap detection, catch-up
  monitoring/   freshness, volume drift, canary runner, the daily health report
  extract/      extraction backfill: sample, diff, commit
  verify.py     the six foundation checks
  cli.py        the `kosh` command
sources/
  amfi-nav-probe/   the throwaway probe that proves the loop end to end
.claude/
  agents/       nine subagent definitions with scoped tool grants
  commands/     /foundation /new-source /health /backfill /gate
docs/
  WORKFLOW.md   how the agents fit together
  FOUNDATION.md what the substrate guarantees and how it was verified
```

## The four rules the code enforces

Not by convention — by schema constraint, tool grant, or a test that fails.

| Rule | Enforced by |
|---|---|
| Observations are append-only | `BEFORE UPDATE/DELETE` triggers in both dialects; `AppendOnlyViolation` |
| Capture and extraction are separate | adapters get a `CaptureContext` with no observation store; `extractor-author` has no network tool; `_validate` rejects mismatched provenance |
| Identity is keyed on ISIN | `entity_id_for` mints `ISIN:` keys; unresolved aliases return `None` and go to the review queue |
| Silence is the dangerous failure | zero rows on an expected day is P0; gap detection; canaries required before scheduling |

## Day-to-day commands

```bash
kosh collect --source <slug> --date 2026-08-21   # capture only, never parses
kosh extract --source <slug> --date 2026-08-21   # parse only, never fetches
kosh run     --source <slug> --date today        # both, plus canaries
kosh health                                      # every morning
kosh gaps    --source <slug>                     # what is missing
kosh catchup --source <slug>                     # fill it, oldest first
kosh backfill diff --source <slug>               # before committing a re-extraction
kosh entities queue                              # unresolved aliases awaiting a human
kosh stats                                       # what the corpus contains
```

## Adding a source

Do not hand-write one. Run the pipeline:

```
/new-source nse-announcements https://www.nseindia.com/companies-listing/corporate-filings-announcements
```

Compliance gate → recon → adapter and canary in parallel → extraction → independent
audit. See [`docs/WORKFLOW.md`](docs/WORKFLOW.md).

A source will not collect until `sources/<slug>/COMPLIANCE.md` exists and carries a
`VERDICT` line that is not `STOP`. That check is in `assert_may_collect`, not in
someone's memory.

## Compliance posture

Kosh states facts. It does not generate buy/sell/hold recommendations, price targets
or ratings — not in code, not in output schemas, not in generated text. It does not
resell or mirror raw exchange data; derived observations are the product. Rate limits
and robots directives are respected by the fetcher itself.
