---
description: Re-run an improved extractor over archived raw captures, or refetch a missed date range. The operation the whole architecture exists to enable.
argument-hint: <source-slug> extract|capture [date-range] — e.g. nse-announcements extract 2026-01-01..2026-08-01
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash, TodoWrite
model: opus
---

Backfill: **$ARGUMENTS**

Two distinct operations. Identify which one is being asked for before doing anything.

## Extraction backfill

Re-run a newer extractor version over blobs already in the store. This is the payoff
of the raw/extract separation: an extractor improved today makes the entire history
better retroactively, at no cost to what is already there.

- **Never delete or update prior Observations.** New rows carry the new `extractor_ver`
  and sit alongside the old ones. CLAUDE.md is absolute on this, and it is what lets
  you compare versions and roll back a bad extractor by filtering rather than restoring.
- Run over a **sample first** — a few hundred captures — and diff v(new) against
  v(old). Investigate every difference before committing to a full run. A backfill that
  quietly changes 40% of a metric's values is either a big improvement or a big
  regression, and you must know which.
- Make it resumable and parallel. Full backfills run for hours over large corpora and
  will be interrupted.
- After completion, spawn `data-auditor` on the newly extracted rows to measure whether
  accuracy actually improved. "The new extractor is better" is a hypothesis until it is
  a measured number.

## Capture backfill

Refetch a date range that was missed. This only works while the source still serves
history — check that first, because many do not, and discovering the window closed
after starting a long run wastes the chance to try alternatives.

- Respect the rate limit in `sources/<slug>/COMPLIANCE.md`. Backfills are the most
  likely thing to get Kosh blocked, since they are exactly the burst traffic that
  rate limiting exists to catch. Go slower than feels necessary.
- Fetching a historical page today is **not** the same observation as fetching it on
  the day — the source may have since corrected or removed things. Set `captured_at`
  to now and `observed_at` to the target date, and flag these captures as backfilled
  so analysis can distinguish them.
- If the source no longer serves the range, say so plainly and record the gap as
  permanent in the source's README rather than leaving it looking fillable.

## Either way

Report before starting: what will run, over how many captures, expected duration, and
what could go wrong. For extraction backfills over more than a few thousand captures,
confirm with the user first.

Report after: counts processed, differences found, audit result, and any capture that
failed and why.
