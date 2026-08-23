# COMPLIANCE — amfi-nav-probe

**VERDICT: PROCEED-WITH-LIMITS**

> **Scope note.** This verdict covers the *foundation probe only*: one fetch per day
> of one public file, to prove the pipeline works end to end. It is deliberately
> narrow. Before AMFI NAV data becomes a real Ring 1 source, run
> `/new-source amfi-nav <url>` so `compliance-reviewer` produces a verdict against the
> live terms rather than inheriting this one. This file is a scaffold, not a legal
> review, and it has not been checked against AMFI's current terms of use.

## Target

- URL: `https://www.amfiindia.com/spages/NAVAll.txt`
- What it is: a plain text file of daily NAVs for every mutual fund scheme in India,
  published by the Association of Mutual Funds in India for public consumption.
- No authentication. No session. No credentials of any kind — Ring 1.

## Limits this source runs under

1. **One fetch per business day.** The file is published once daily; polling it more
   often gains nothing and costs AMFI bandwidth. Enforced by the `daily` cadence and
   the per-host interval in `core/scheduler/fetch.py`.
2. **No mirroring or redistribution of the raw file.** The blob is stored locally as
   provenance for our own observations and is never republished. Derived observations
   are the product (CLAUDE.md: "never resell or mirror raw exchange data" — the same
   principle applies here).
3. **Identify honestly.** `KOSH_USER_AGENT` carries a contact address. Set it.
4. **Respect robots.txt.** Enforced in the fetcher, not by convention.

## SEBI check

Observations from this source are statements of fact: "scheme X had NAV Y on date Z".
No recommendation, rating, target or ranking is derived, stored or emitted. Nothing
here approaches SEBI Research Analyst territory.

## Open questions for the real review

- AMFI's terms of use page — read it and quote the redistribution clause here.
- Whether a historical NAV endpoint exists and under what conditions (matters for
  capture backfill, which is time-limited and therefore urgent).
