---
description: Run a phase gate from the roadmap — an independent, adversarial check on whether a quarter's milestone was genuinely met before moving to the next ring.
argument-hint: <q1|q2|q3|q4>
allowed-tools: Task, Read, Write, Glob, Grep, Bash, TodoWrite
model: opus
---

Run the phase gate for: **$ARGUMENTS**

Gates exist because the failure mode of a twelve-month solo project is drifting into
the next phase on the strength of a phase that only mostly works. Your job here is to
be genuinely adversarial about whether the milestone was met. A gate that always passes
is not a gate.

## The thresholds

**Q1 — Ring 1 collecting.** 90 consecutive days of unattended collection with under 2%
missing-day rate across scheduled sources. At least five Ring 1 sources live. The corpus
answers a real query end to end — e.g. every announcement a given company filed this
quarter, with each fact traceable to a raw capture.

**Q2 — Document intelligence.** 100 companies with 3 years of reports parsed. Above 95%
accuracy on a manual 50-fact audit, reported per fact type rather than blended. Citations
resolve — spot-check by opening the cited page.

**Q3 — Alt-data and query layer.** At least one alt-data series with 8+ weeks of
history. The natural-language layer answers with citations and an explicit "as of" date.
The leading-indicator chart from the roadmap either exists or does not — be honest, since
this one is the pitch.

**Q4 — Ring 3 and distribution.** Portfolio sync working local-first with no credential
material at rest anywhere it shouldn't be. The portfolio-to-corpus join produces a real
answer.

## How to run it

Spawn `data-auditor` for an independent measurement against the relevant threshold —
do not measure it yourself, and do not accept the authoring agents' own accuracy claims.

Then check the things audits miss:

- **Is collection actually unattended?** A pipeline that needs a weekly manual nudge
  has not met a 90-day unattended bar, however good the data looks.
- **Are the canaries real?** Sample a few and confirm each has been seen to fail. A
  check that has never fired proves nothing.
- **Has anything been silently abandoned?** Sources that were enabled and quietly
  stopped, extractors left at low accuracy, queue depth in entity resolution growing
  unboundedly.
- **Does the compliance trail hold?** Every live source has a current `COMPLIANCE.md`,
  and nothing in the output has drifted toward being a recommendation.

## Verdict

Write `docs/gates/<phase>.md` with the measured numbers, the verdict, and — on a fail —
the specific shortest path to passing.

Report PASS or FAIL plainly to the user. On a fail, do not soften it and do not suggest
proceeding anyway with caveats; name what is missing and roughly how long it will take.
Passing a gate that was not met costs a whole quarter later, and this project's whole
value depends on the corpus underneath being trustworthy.
