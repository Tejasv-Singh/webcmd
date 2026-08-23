---
description: Run the full pipeline to add a new data source to Kosh — compliance gate, reconnaissance, adapter and canary in parallel, extraction, then independent audit.
argument-hint: <slug> <url-or-name> — e.g. nse-announcements https://www.nseindia.com/companies-listing/corporate-filings-announcements
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash, TodoWrite
model: opus
---

Add a new data source to Kosh: **$ARGUMENTS**

You are the orchestrator. You do not write the adapter, the extractor, or the checks
yourself — you run the pipeline, enforce the gates, and decide what happens when a
stage comes back unhappy.

## Before anything

Read `CLAUDE.md` and `docs/WORKFLOW.md`. Create a todo list for the stages below so
progress is visible. Create `sources/<slug>/`.

Subagents share no context with you or with each other. **The filesystem is the bus** —
each stage reads its input from disk and writes its output to disk. Before advancing a
stage, confirm the expected file actually exists. If it does not, that stage did not
complete, regardless of what its summary said.

## Stage 1 — Compliance gate (blocking)

Spawn `compliance-reviewer` with the source slug, the target URL, and a one-line
statement of what Kosh intends to collect.

**Do not proceed until `sources/<slug>/COMPLIANCE.md` exists.** Read it yourself rather
than trusting the summary.

- **STOP** → halt the pipeline. Report to the user what was prohibited and what the
  reviewer suggested as an alternative source. Do not look for a workaround.
- **PROCEED-WITH-LIMITS** → normal. Carry the binding limits forward into every
  later stage.
- **PROCEED** → continue.

## Stage 2 — Reconnaissance (blocking)

Spawn `source-scout` with the slug and the compliance limits.

Wait for `sources/<slug>/BRIEF.md`. Read it and check it is actually implementable —
does it name a concrete access path, real sample bytes, real volume numbers, and
pagination mechanics? A vague brief is the main cause of rework downstream, so send
it back for a second pass rather than letting two agents build on guesswork.

If the scout found a JSON endpoint, note it — that materially changes what stages 3
and 4 look like, for the better.

## Stage 3 — Adapter and canary (parallel)

Spawn **both in the same message so they run concurrently**:

- `adapter-author` — builds the fetch-and-persist adapter from the brief.
- `canary-author` — writes the checks from the brief.

They are independent by design. canary-author deliberately does not read the adapter
source, so its checks test reality rather than the implementation. Do not serialize
them and do not let either see the other's output.

When both return, verify `sources/<slug>/adapter/` and `sources/<slug>/canary/` exist
and that the adapter has actually landed blobs in the store. If the adapter reports
that the brief was wrong in some respect, confirm the correction was written back into
`BRIEF.md` — the brief is the durable record.

## Stage 4 — Extraction (blocking)

Spawn `extractor-author`. It works offline against the blobs stage 3 captured.

If it reports that the captures lack something it needs, **do not let it work around
the gap** — that is the architecture telling you the adapter under-captured. Send it
back to `adapter-author` to capture more, then re-run extraction.

If unresolved entity aliases came out of extraction, spawn `entity-resolver` to work
the queue before auditing.

## Stage 5 — Audit gate (blocking)

Spawn `data-auditor` with a threshold of **95% accuracy on a stratified sample of at
least 30 facts**.

- **PASS** → continue to stage 6.
- **FAIL** → route each finding to the agent it was attributed to (extractor bugs to
  `extractor-author`, capture bugs to `adapter-author`, join errors to
  `entity-resolver`), then re-audit. Do not fix the findings yourself — the
  independence of the audit is the point. Do not lower the threshold to get a pass.

## Stage 6 — Schedule and land

Once the audit passes: register the adapter's cadence with the scheduler, enable its
canaries in monitoring, and confirm the source appears in the freshness dashboard.

Run one full scheduled cycle end to end and confirm data lands.

## Stage 7 — Report

Summarize for the user in a few sentences: what the source provides, the access path,
expected daily volume, measured accuracy, and anything still fragile. Note whether this
adapter is a candidate to publish as a Webcmd community plugin — public-data adapters
generally are, and the catalog is nearly empty.

## Throughout

If a stage fails twice on the same problem, stop and ask the user rather than trying a
third variation. Never skip the compliance or audit gates to save time; they are the
two stages whose absence is expensive months later rather than minutes later.
