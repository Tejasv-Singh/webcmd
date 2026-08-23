---
description: Month-0 build — the observation store, blob store, entity graph, scheduler and monitoring that everything else plugs into. Run once, before any source work.
argument-hint: (no arguments)
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash, TodoWrite
model: opus
---

Build the Kosh foundation. This runs once, before any adapter exists.

The goal of this phase is **not** coverage. It is a spine correct enough that the next
eleven months are additive rather than corrective, and a capture loop running early
enough that the corpus clock starts. Read `CLAUDE.md` first — the five architecture
rules there are the specification for this work.

Create a todo list and work the stages in order.

## Stage 1 — Core substrate

Spawn `pipeline-engineer` to build: the append-only observation store with append-only
enforced in the schema itself, the content-addressed blob store, the scheduler with
gap detection and catch-up, extraction-backfill machinery, and the monitoring surface.

The single most important property to verify: **an extractor can be re-run over an
archived blob and produce Observations that sit alongside the originals rather than
replacing them.** If that does not work, nothing else in the architecture pays off.

## Stage 2 — Entity graph

Spawn `entity-resolver` to build the ISIN-keyed graph and seed it with the Nifty 500,
including NSE symbols, BSE codes and legal names, plus date-aware resolution and the
identity-event model for name changes, symbol changes, mergers and delistings.

This runs before any source because every source depends on it, and retrofitting entity
resolution later is a rewrite rather than a patch.

## Stage 3 — End-to-end validation

Build one deliberately throwaway adapter against **AMFI's daily NAV file** — it is a
plain public file, needs no browser, and exists purely to prove the pipeline works
end to end. Do not invest in it; it is a test harness, not a Ring 1 source.

Verify the full loop yourself:

1. Fetch lands a blob in the store, content-addressed.
2. Fetching identical bytes again deduplicates to one blob with two capture records.
3. An extractor turns that blob into Observations with correct `observed_at` versus
   `captured_at` and correct IST-to-UTC handling.
4. Re-running the extractor at a bumped version produces new rows alongside the old ones,
   with both versions queryable.
5. Killing a run mid-capture and restarting leaves the store consistent.
6. Simulating a missed day causes the scheduler's gap detection to catch up automatically.

## Stage 4 — Gate

Do not declare foundation complete until all six checks above pass. Report to the user
what was built, what you verified, and the exact command to run collection for a single
source and a single date — you will use that constantly from here on.

Then say plainly what the next step is: `/new-source` for the first real Ring 1 source,
which should be NSE and BSE announcements, since they are the highest-signal and the
most annoying to get.
