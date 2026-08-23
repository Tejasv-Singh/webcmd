"""Gap detection and catch-up.

The failure this exists for is CLAUDE.md rule 4: a scraper that crashes is a nuisance,
one that quietly stops is a catastrophe. Gap detection turns "nobody noticed for three
weeks" into a number that shows up every morning in `kosh health`.

Capture gaps are time-limited in a way extraction gaps are not: NSE will not serve you
last month's announcements page forever, so a capture gap has to be filled fast or it
becomes permanent. That urgency is why `catchup` runs oldest-first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from core.config import settings
from core.scheduler.calendar import calendar_warnings, expected_dates
from core.scheduler.registry import SourceRegistry, SourceSpec
from core.store.db import Database
from core.timeutil import ist_today

if TYPE_CHECKING:  # pragma: no cover
    from core.scheduler.runner import Runner


@dataclass
class GapReport:
    slug: str
    calendar: str
    first_date: date | None
    last_date: date | None
    expected: list[date]
    captured: list[date]
    missing: list[date]
    warnings: list[str]

    @property
    def missing_rate(self) -> float:
        return len(self.missing) / len(self.expected) if self.expected else 0.0

    def summary(self) -> str:
        if not self.expected:
            return f"{self.slug}: nothing collected yet"
        return (
            f"{self.slug}: {len(self.captured)}/{len(self.expected)} expected days "
            f"({self.missing_rate:.1%} missing)"
        )


def captured_dates(db: Database, slug: str) -> list[date]:
    """Dates with at least one successful capture. A run that errored does not count
    as coverage even if it happened to write a blob first."""
    rows = db.query(
        "SELECT DISTINCT target_date AS d FROM captures"
        " WHERE source_slug = ? AND target_date IS NOT NULL ORDER BY d",
        (slug,),
    )
    return [date.fromisoformat(r["d"]) if isinstance(r["d"], str) else r["d"] for r in rows]


def detect(
    db: Database,
    spec: SourceSpec,
    *,
    since: date | None = None,
    through: date | None = None,
) -> GapReport:
    have = captured_dates(db, spec.slug)
    through = through or ist_today()
    start = since or (have[0] if have else None)
    if start is None:
        return GapReport(spec.slug, spec.calendar, None, None, [], [], [], [])
    expected = expected_dates(start, through, spec.calendar)
    have_set = set(have)
    missing = [d for d in expected if d not in have_set]
    return GapReport(
        slug=spec.slug,
        calendar=spec.calendar,
        first_date=have[0] if have else None,
        last_date=have[-1] if have else None,
        expected=expected,
        captured=[d for d in expected if d in have_set],
        missing=missing,
        warnings=calendar_warnings(spec.calendar, through),
    )


def detect_all(db: Database, *, enabled_only: bool = True) -> list[GapReport]:
    reg = SourceRegistry(db)
    return [detect(db, spec) for spec in reg.all(enabled_only=enabled_only)]


def catchup(
    runner: Runner,
    slug: str,
    *,
    max_days: int | None = None,
    through: date | None = None,
    extract: bool = True,
) -> list[dict[str, Any]]:
    """Fill detected gaps, oldest first.

    Oldest first because the oldest gap is the one closest to becoming permanent. The
    cap exists so a misconfigured calendar cannot turn into a thousand requests at the
    source; when it bites, that is a signal to look, not to raise the cap.
    """
    reg = SourceRegistry(runner.db)
    spec = reg.get(slug)
    if spec is None:
        raise KeyError(f"source {slug} is not registered")
    cap = max_days if max_days is not None else settings().max_catchup_days
    report = detect(runner.db, spec, through=through)
    todo = report.missing[:cap]
    results = []
    for day in todo:
        run = runner.collect(slug, day)
        entry: dict[str, Any] = {
            "date": day,
            "capture": run.status,
            "blobs": run.blob_count,
            "error": run.error,
        }
        if extract and run.ok:
            ex = runner.extract(slug, day)
            entry["extract"] = ex.status
            entry["rows"] = ex.row_count
            entry["error"] = entry["error"] or ex.error
        results.append(entry)
    if len(report.missing) > len(todo):
        results.append(
            {
                "date": None,
                "capture": "skipped",
                "blobs": 0,
                "error": (
                    f"{len(report.missing) - len(todo)} older gaps left untouched by the "
                    f"{cap}-day catch-up cap. If these are real, they may already be "
                    "unrecoverable at the source -- check before raising the cap."
                ),
            }
        )
    return results


def unextracted_captures(db: Database, slug: str, version: str | None = None) -> list[str]:
    """Captures with no observations at all -- or none at a given extractor version.

    The second form is what an extraction backfill iterates over after a version bump.
    """
    if version:
        sql = (
            "SELECT c.capture_id FROM captures c WHERE c.source_slug = ? AND NOT EXISTS"
            " (SELECT 1 FROM observations o WHERE o.capture_id = c.capture_id"
            "  AND o.extractor_ver = ?) ORDER BY c.captured_at"
        )
        params: tuple = (slug, version)
    else:
        sql = (
            "SELECT c.capture_id FROM captures c WHERE c.source_slug = ? AND NOT EXISTS"
            " (SELECT 1 FROM observations o WHERE o.capture_id = c.capture_id)"
            " ORDER BY c.captured_at"
        )
        params = (slug,)
    return [r["capture_id"] for r in db.query(sql, params)]


def freshness(db: Database, slug: str) -> dict[str, Any]:
    """How long since this source last produced anything."""
    from core.timeutil import parse_ts

    row = db.one("SELECT MAX(captured_at) AS t FROM captures WHERE source_slug = ?", (slug,))
    last = parse_ts(row["t"]) if row and row.get("t") else None
    if last is None:
        return {"slug": slug, "last_capture": None, "age_hours": None}
    from core.timeutil import utcnow

    return {
        "slug": slug,
        "last_capture": last,
        "age_hours": round((utcnow() - last).total_seconds() / 3600, 1),
    }


def next_due(spec: SourceSpec, last: date | None, *, today: date | None = None) -> date | None:
    """The next date this source is expected to run for."""
    today = today or ist_today()
    start = (last + timedelta(days=1)) if last else today
    for d in expected_dates(start, today, spec.calendar):
        return d
    return None
