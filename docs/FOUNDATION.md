# The foundation

What `/foundation` builds, what it guarantees, and how those guarantees are checked.

Everything here is verifiable on a clean machine with nothing but Python:

```bash
python -m core.cli verify-foundation
```

## The one property that matters

> Re-running an extractor at a bumped version produces new rows **alongside** the old
> ones, with both versions queryable.

If that does not hold, the "improve the extractor in month nine and re-run it over ten
months of archive" property is absent, and every month of collection after it is built
on sand. It is check 4, and it is the reason the store is append-only and the reason
raw capture is a separate step from extraction.

## The stores

### Raw blobs — content-addressed

`core/store/blobs.py`. Bytes are keyed by sha256 and fanned out two levels
(`ab/cd/<sha>`). Fetching identical content twice stores **one blob and two capture
records**: the full fetch history is kept without paying for it twice.

The write order is deliberate — file first (via temp file plus atomic rename), row
second. A killed process can therefore leave an orphan blob on disk with no capture
row, which is recoverable; the reverse, a row pointing at bytes that are not there, is
not.

`blobs.verify()` re-hashes stored blobs and reports any that no longer match their
key. `blobs.read()` raises rather than returning empty when a blob is missing, because
a missing blob is data loss and extraction must not quietly route around it.

### Observations — append-only

`core/store/observations.py`. The schema itself refuses `UPDATE` and `DELETE`:

- **SQLite** — `BEFORE UPDATE` / `BEFORE DELETE` triggers calling `RAISE(ABORT, ...)`.
- **Postgres** — a `kosh_append_only()` trigger function raising an exception.

Both surface as `AppendOnlyViolation` so callers and tests never have to know which
dialect they are on. Protected tables: `observations`, `captures`, `raw_blobs`
(delete), `identity_events`.

`runs`, `sources`, `entities`, `entity_aliases` and the review queue are deliberately
mutable. They are how we *run* the system, not what we *know* about the world.

Uniqueness is `(capture_id, entity_id, metric, observed_at, extractor_ver)`. That one
line is what makes re-running the same version idempotent while a bumped version
appends alongside.

**A correction is a new row with a later `captured_at`**, never an edit. `known_at=`
on any query gives point-in-time reconstruction: what Kosh actually believed on a past
date, which is what makes a backtest honest.

### Time

Stored UTC, displayed IST, and the two are never conflated:

- `observed_at` — when the fact was true (an IST market timestamp, converted).
- `captured_at` — when we saw it.
- `inserted_at` — when the row was written.

`core/timeutil.py` rejects naive datetimes outright rather than guessing at a zone.

## The entity graph

`core/entities/graph.py`. Keyed on ISIN wherever one exists (`ISIN:INE009A01021`),
`ORG:<slug>` only where one genuinely does not (a private company whose careers page
we watch).

Aliases are **date-scoped**: `valid_from` / `valid_to`. A symbol change closes the old
window and opens a new one, so a document filed in 2024 still resolves correctly in
2026. Identity events (`name_change`, `symbol_change`, `merger`, `demerger`,
`delisting`, `relisting`) are append-only.

Resolution never guesses. Zero matches and ambiguous matches both return `None`, and
`resolve_or_queue` puts the miss in the review queue where a human can see it. An
alias that already points at a different entity raises rather than being silently
reassigned.

Seeding: `kosh entities seed-nse` pulls NSE's published `EQUITY_L.csv`, which carries
real ISINs. **No ISIN in this repository is invented** — the sample seed is four
obviously-fake `INE000TEST*` entities, and a test asserts it stays that way.

## The scheduler

`core/scheduler/`. Three calendars: `daily`, `weekday`, `nse_trading`. The trading
calendar reads `core/scheduler/holidays/nse.csv`, which ships **empty on purpose** —
guessing at exchange holidays either fabricates a P0 or hides a real outage.
`kosh health` warns whenever the current year has no holidays loaded.

Gap detection compares expected days against days with a successful capture.
`kosh catchup` fills them **oldest first**, because capture gaps expire: sources stop
serving history, and the oldest gap is the one closest to becoming permanent. The
catch-up cap exists so a misconfigured calendar cannot turn into a thousand requests
at the source; when it bites, that is a signal to look, not to raise the cap.

The fetcher enforces a per-host interval and `robots.txt` in code, because those are
the things that get "temporarily" skipped at 1am during a backfill.

## Monitoring

`core/monitoring/checks.py`, and the severity model is deliberately asymmetric:

- **Zero rows on an expected day is P0** before anyone has looked at it, and stays P0
  until a human checks the *source* and confirms it was genuinely quiet.
- Volume drift is measured against a trailing **median**, so one bad day cannot move
  the baseline enough to hide the next one.
- A source with no canary module is a finding in itself — canaries ship *before*
  scheduling.
- A source directory that is not registered is reported, because that is how work gets
  silently abandoned.
- Killed runs are reported, never silently reaped.

## The six checks

Run against a throwaway store using the **real** probe adapter and extractor, with only
the socket stubbed (`--live` uses the real one):

| # | Check |
|---|---|
| 1 | fetch lands a blob in the store, content-addressed |
| 2 | identical bytes deduplicate to one blob with two capture records |
| 3 | extraction produces Observations with correct `observed_at` vs `captured_at` and IST→UTC handling |
| 4 | re-running at a bumped version appends alongside; both versions queryable; `UPDATE`/`DELETE` blocked by the schema |
| 5 | killing a run mid-capture and restarting leaves the store consistent |
| 6 | a missed day is detected by gap detection and filled by catch-up |

Current status: **all six pass.**

## What the foundation deliberately does not do

- No real Ring 1 source. `amfi-nav-probe` is a test harness; when NAVs matter, run
  `/new-source amfi-nav` and let the pipeline produce a proper brief and verdict.
- No NSE holiday list. Paste it from the exchange circular.
- No index membership. Constituents change; that is a versioned observation, not a
  seed file.
- No scheduling daemon. `kosh run` is what cron (or the Task Scheduler) calls.
