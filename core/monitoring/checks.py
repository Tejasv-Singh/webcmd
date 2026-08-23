"""Monitoring: freshness, volume drift, canaries.

CLAUDE.md rule 4 is the whole reason this module exists. A crash is loud and gets
fixed. A source that starts returning an empty list on a trading day is silent, and
three weeks later the corpus has a hole in it that cannot be refilled.

So the severity model here is deliberately asymmetric: zero rows on an expected day is
P0 before anyone has looked at it, and it stays P0 until a human checks the *source*
and says it was genuinely quiet. Assuming a quiet day is how you lose a month.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from core.config import settings
from core.scheduler.calendar import is_expected
from core.scheduler.gaps import GapReport, detect, freshness
from core.scheduler.registry import SourceRegistry, SourceSpec, load_canary
from core.store.db import Database
from core.timeutil import ist_today, parse_ts, utcnow

SEVERITIES = ("P0", "P1", "P2", "INFO")


@dataclass
class Finding:
    severity: str
    slug: str
    check: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.slug}: {self.message}"


@dataclass
class CanaryResult:
    """What a source's canary module returns. Status is 'pass', 'fail' or 'warn'."""

    name: str
    status: str
    detail: str = ""


class CanaryContext:
    """What a canary is handed.

    Note what is *not* here: the adapter module. Canaries are written from the source
    brief and check reality -- row counts, value ranges, field presence -- so that a
    canary and the adapter cannot share the same wrong assumption.
    """

    def __init__(self, db: Database, slug: str, target_date: date | None = None) -> None:
        self.db = db
        self.slug = slug
        self.target_date = target_date or ist_today()

    def observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        from core.store.observations import ObservationStore

        return ObservationStore(self.db).query(**kwargs)

    def row_count(self, target_date: date | None = None) -> int:
        d = target_date or self.target_date
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM observations o JOIN captures c"
                " ON c.capture_id = o.capture_id"
                " WHERE c.source_slug = ? AND c.target_date = ?",
                (self.slug, d),
            )
            or 0
        )

    def latest_captures(self, limit: int = 5) -> list[dict[str, Any]]:
        from core.store.blobs import BlobStore

        return BlobStore(self.db).captures(self.slug, limit=limit)

    def metric_values(self, metric: str, target_date: date | None = None) -> list[Any]:
        d = target_date or self.target_date
        rows = self.db.query(
            "SELECT o.value_num AS v FROM observations o JOIN captures c"
            " ON c.capture_id = o.capture_id"
            " WHERE c.source_slug = ? AND c.target_date = ? AND o.metric = ?",
            (self.slug, d, metric),
        )
        return [r["v"] for r in rows if r["v"] is not None]

    @staticmethod
    def ok(name: str, detail: str = "") -> CanaryResult:
        return CanaryResult(name, "pass", detail)

    @staticmethod
    def fail(name: str, detail: str) -> CanaryResult:
        return CanaryResult(name, "fail", detail)

    @staticmethod
    def warn(name: str, detail: str) -> CanaryResult:
        return CanaryResult(name, "warn", detail)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_zero_rows(db: Database, spec: SourceSpec, day: date | None = None) -> list[Finding]:
    day = day or ist_today() - timedelta(days=1)
    if not is_expected(day, spec.calendar):
        return []
    n = (
        db.scalar(
            "SELECT COUNT(*) FROM observations o JOIN captures c ON c.capture_id ="
            " o.capture_id WHERE c.source_slug = ? AND c.target_date = ?",
            (spec.slug, day),
        )
        or 0
    )
    if n:
        return []
    return [
        Finding(
            "P0",
            spec.slug,
            "zero-rows",
            f"zero observations for {day} ({spec.calendar} calendar says this day should "
            "have data). Treat as breakage until you have checked the source itself -- "
            "not until it looks plausible.",
            {"date": str(day)},
        )
    ]


def check_freshness(db: Database, spec: SourceSpec) -> list[Finding]:
    f = freshness(db, spec.slug)
    if f["last_capture"] is None:
        return [
            Finding("P1", spec.slug, "freshness", "registered and enabled but never captured")
        ]
    tolerance = {"daily": 36, "weekly": 24 * 8, "monthly": 24 * 32}.get(spec.cadence, 36)
    if f["age_hours"] > tolerance:
        sev = "P0" if f["age_hours"] > tolerance * 2 else "P1"
        return [
            Finding(
                sev,
                spec.slug,
                "freshness",
                f"last capture was {f['age_hours']}h ago, tolerance for a "
                f"{spec.cadence} source is {tolerance}h",
                f,
            )
        ]
    return []


def check_drift(db: Database, spec: SourceSpec) -> list[Finding]:
    """Volume drift against the trailing median.

    Median rather than mean: one bad day should not move the baseline enough to hide
    the next one.
    """
    cfg = settings()
    rows = db.query(
        "SELECT c.target_date AS d, COUNT(*) AS n FROM observations o"
        " JOIN captures c ON c.capture_id = o.capture_id"
        " WHERE c.source_slug = ? AND c.target_date IS NOT NULL"
        " GROUP BY c.target_date ORDER BY c.target_date DESC"
        f" LIMIT {int(cfg.drift_window) + 1}",
        (spec.slug,),
    )
    if len(rows) < 4:
        return []
    latest, history = rows[0], rows[1:]
    baseline = statistics.median(r["n"] for r in history)
    if baseline == 0:
        return []
    delta = (latest["n"] - baseline) / baseline
    if abs(delta) <= cfg.drift_threshold:
        return []
    direction = "above" if delta > 0 else "below"
    return [
        Finding(
            "P1" if delta > 0 else "P0",
            spec.slug,
            "volume-drift",
            f"{latest['n']} rows on {latest['d']} is {abs(delta):.0%} {direction} the "
            f"{len(history)}-day median of {baseline:.0f}",
            {"latest": latest["n"], "median": baseline, "delta": round(delta, 3)},
        )
    ]


def run_canaries(
    db: Database, spec: SourceSpec, day: date | None = None, *, persist: bool = True
) -> list[Finding]:
    module = load_canary(spec.slug)
    if module is None:
        return [
            Finding(
                "P1",
                spec.slug,
                "canary-missing",
                "no canary module. A source ships with canaries *before* it goes into "
                "the schedule (CLAUDE.md rule 4).",
            )
        ]
    ctx = CanaryContext(db, spec.slug, day)
    findings: list[Finding] = []
    try:
        results = module.run(ctx)
    except Exception as exc:
        return [
            Finding(
                "P1", spec.slug, "canary-error", f"canary raised {type(exc).__name__}: {exc}"
            )
        ]
    for res in results:
        if persist:
            db.execute(
                "INSERT INTO canary_results (source_slug, check_name, status, detail,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (spec.slug, res.name, res.status, res.detail, utcnow()),
            )
        if res.status == "fail":
            findings.append(Finding("P0", spec.slug, f"canary:{res.name}", res.detail))
        elif res.status == "warn":
            findings.append(Finding("P2", spec.slug, f"canary:{res.name}", res.detail))
    if persist:
        db.commit()
    return findings


def check_gaps(db: Database, spec: SourceSpec) -> tuple[GapReport, list[Finding]]:
    report = detect(db, spec)
    findings = [Finding("P2", spec.slug, "calendar", w) for w in report.warnings]
    recent = [d for d in report.missing if d >= ist_today() - timedelta(days=7)]
    if recent:
        findings.append(
            Finding(
                "P0",
                spec.slug,
                "gap",
                f"{len(recent)} missing day(s) in the last week: "
                f"{', '.join(str(d) for d in recent[:5])}. Capture gaps expire -- "
                "sources stop serving history. Run `kosh catchup` now.",
                {"missing": [str(d) for d in recent]},
            )
        )
    older = [d for d in report.missing if d not in recent]
    if older:
        findings.append(
            Finding(
                "P2",
                spec.slug,
                "gap-historic",
                f"{len(older)} older missing day(s); may already be unrecoverable",
                {"count": len(older)},
            )
        )
    return report, findings


def check_stale_runs(db: Database) -> list[Finding]:
    from core.scheduler.runner import Runner

    out = []
    for row in Runner(db).stale_runs():
        out.append(
            Finding(
                "P1",
                row["source_slug"],
                "stale-run",
                f"run {row['run_id'][:8]} ({row['phase']}) has been 'running' since "
                f"{row['started_at']} -- the process was probably killed",
            )
        )
    return out


def check_unscheduled_sources(db: Database) -> list[Finding]:
    """A source directory nobody enabled. This is how work gets silently abandoned."""
    reg = SourceRegistry(db)
    registered = {s.slug for s in reg.all()}
    findings = []
    for slug in reg.discover():
        if slug not in registered:
            findings.append(
                Finding(
                    "P2",
                    slug,
                    "unregistered",
                    "source directory exists but is not registered; built and forgotten?",
                )
            )
    for spec in reg.all():
        if not spec.enabled:
            findings.append(
                Finding("INFO", spec.slug, "disabled", "registered but not enabled")
            )
    return findings


# ---------------------------------------------------------------------------
# The daily report
# ---------------------------------------------------------------------------


@dataclass
class HealthReport:
    generated_at: Any
    findings: list[Finding]
    gaps: list[GapReport]
    sources: list[dict[str, Any]]

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def worst(self) -> str:
        for sev in SEVERITIES:
            if self.by_severity(sev):
                return sev
        return "OK"


def health(db: Database, *, day: date | None = None, run_canary: bool = True) -> HealthReport:
    reg = SourceRegistry(db)
    findings: list[Finding] = []
    gaps: list[GapReport] = []
    rows: list[dict[str, Any]] = []

    for spec in reg.all(enabled_only=True):
        findings += check_freshness(db, spec)
        findings += check_zero_rows(db, spec, day)
        findings += check_drift(db, spec)
        report, gap_findings = check_gaps(db, spec)
        gaps.append(report)
        findings += gap_findings
        if run_canary:
            findings += run_canaries(db, spec, day)
        f = freshness(db, spec.slug)
        rows.append(
            {
                "slug": spec.slug,
                "ring": spec.ring,
                "cadence": spec.cadence,
                "last_capture": f["last_capture"],
                "age_hours": f["age_hours"],
                "coverage": f"{len(report.captured)}/{len(report.expected)}"
                if report.expected
                else "-",
            }
        )

    findings += check_stale_runs(db)
    findings += check_unscheduled_sources(db)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.slug))
    return HealthReport(utcnow(), findings, gaps, rows)


def last_canary_fire(db: Database, slug: str | None = None) -> dict[str, Any] | None:
    """Used by `/gate`: a canary that has never once fired is not evidence of health,
    it is an untested assertion."""
    sql = "SELECT * FROM canary_results WHERE status = 'fail'"
    params: list[Any] = []
    if slug:
        sql += " AND source_slug = ?"
        params.append(slug)
    sql += " ORDER BY created_at DESC LIMIT 1"
    row = db.one(sql, params)
    if row:
        row["created_at"] = parse_ts(row["created_at"])
    return row
