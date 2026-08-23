# Setup

Kosh is two things in one repo: the **workflow** that builds the corpus (nine subagents
and five slash commands under `.claude/`), and the **substrate** that workflow builds
against (`core/`, plus the AMFI probe under `sources/`).

The foundation is built and verified — `python -m core.cli verify-foundation` runs the
six checks in under a second on a stock Python, with no services and no network. See
[`docs/FOUNDATION.md`](docs/FOUNDATION.md) for what it guarantees and
[`README.md`](README.md) for the day-to-day commands.

## Install

```bash
python -m core.cli verify-foundation    # six checks, no dependencies
python -m pytest -q                     # 103 tests, offline
```

Then, in Claude Code:

```bash
claude
/agents          # confirm all nine agents are registered
/new-source nse-announcements https://www.nseindia.com/companies-listing/corporate-filings-announcements
```

`/foundation` has already been run; re-running it would rebuild what is in `core/`.

## What's in here

**`CLAUDE.md`** — inherited by every agent on every spawn. It carries the five
non-negotiable architecture rules, the ring build order, the compliance constraints
that shape the code, and the repo layout. This file is doing most of the work; the
agent definitions largely enforce what it declares. Edit it as decisions change,
and treat it as the project's constitution rather than as notes.

**`.claude/agents/`** — nine subagent definitions, each with a scoped tool grant.
The grants are load-bearing, not cosmetic: `extractor-author` has no network or browser
precisely so it *cannot* violate the raw/extract separation. Don't widen a grant
without understanding what boundary it was enforcing.

**`.claude/commands/`** — five orchestration commands. `/new-source` is the centrepiece
and the one you'll run dozens of times; the rest support it.

**`docs/WORKFLOW.md`** — how it all fits together, with the pipeline diagram and the
known failure modes.

## Prerequisites

**For the substrate:** Python 3.11+. That is the whole list — the SQLite path has zero
runtime dependencies on purpose, because "I couldn't install the deps" is not an
acceptable reason to miss a day of collection.

**For real collection, additionally:**

- Postgres for the observation store (`docker compose up -d` gives you one on 5433)
- Node 20.6+ and `npm install -g @agentrhq/webcmd`, then `webcmd doctor` — needed by
  `source-scout` and by any adapter whose source requires a browser
- `webcmd skills add` → choose Claude, so agents get the webcmd usage skill
- `uv` for dev tooling (`make install` sets up ruff and pytest)

## Order of operations

1. ~~`/foundation`~~ — done. `kosh verify-foundation` re-checks it any time; treat a
   red check as a stop-work condition rather than a known issue.
2. `/new-source nse-announcements <url>` — the first real source. Announcements first,
   because they're the highest-signal and the most annoying to get, so you learn the
   most from doing them early.
3. `/health` daily from then on.
4. `/gate q1` at 90 days.

Retire `amfi-nav-probe` once a real source is collecting. It is a test harness, and the
main risk it carries is becoming the de facto AMFI source by accident.

## Tuning it

The thresholds are opinions, and reasonable ones, but they're yours to change: the 95%
audit bar in `/new-source`, the 2% missing-day rate in `/gate`, the model assignments
in each agent's frontmatter. Change them in the files deliberately rather than
overriding them in the moment — the whole point of a gate is that it isn't negotiable
under pressure.

If you find yourself repeatedly giving an agent the same correction, that correction
belongs in its definition file or in `CLAUDE.md`. The scaffold should get better every
time you use it; a workflow that needs the same nudge twice is a workflow with a
missing line.
