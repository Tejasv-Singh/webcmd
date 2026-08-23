---
description: Morning collection health check — freshness, canary results, drift and gaps across every source. Run daily; it is the routine that catches silent breakage.
argument-hint: [source-slug] — omit to check all sources
allowed-tools: Task, Read, Glob, Grep, Bash, TodoWrite
model: sonnet
---

Check collection health for: **${ARGUMENTS:-all sources}**

CLAUDE.md classifies silence as the dangerous failure. This command is the routine
that makes silence visible, so run it even on days when nothing seems wrong —
especially then.

## What to check

**Freshness.** For every source, when did data last land, and is that within its
declared cadence? Account for the Indian market calendar — a trading-day source that
is quiet on a market holiday is healthy, and treating that as an incident is how
people learn to ignore these alerts.

**Canary results.** Read the latest run for every source. Report P0s first and
individually; group P1s; note P2 counts without detail.

**Volume drift.** Compare each source's recent counts against its trailing 30-day
median for the same day-type. Flag anything outside band in either direction — an
unexplained *increase* often means duplicate captures, which is as much a defect as
a shortfall.

**Gaps.** Reconcile expected collection days against actual for the last 30 days per
source. Any gap that the scheduler's catch-up has not already filled is urgent,
because some sources stop serving history after a window and a gap left too long
becomes permanent.

**Run ledger.** Failed runs, retry storms, and rate-limit responses. A source
quietly retrying its way through every run is about to be blocked.

## Zero-row days

Treat any source returning zero rows on a day it should have data as **P0 until proven
otherwise**, and prove it by checking the source directly rather than by assuming it
was a quiet day. This is the exact failure mode the project is built to avoid.

## Report

Lead with anything requiring action today, then the all-clear detail. Be specific:
name the source, the metric, the expected value and the observed one. If everything
is healthy, say so in one line — do not pad a clean report.

For anything needing investigation beyond a glance, spawn `data-auditor` on that
source rather than diagnosing it inline here.
