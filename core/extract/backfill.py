"""Extraction backfill: re-run an improved extractor over archived captures.

This is the payoff of CLAUDE.md rule 2. Everything else in the repo is in service of
being able to run this and have ten months of history get better retroactively.

The order is deliberate and it is not optional: **sample, diff, investigate, then
commit.** A backfill that runs straight over the whole corpus and reports "42,000 new
rows" has told you nothing about whether those rows are better. And because the store
is append-only, a bad backfill cannot be undone -- it can only be superseded by yet
another version, with the wrong rows sitting in the history forever.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.scheduler.registry import load_extractor
from core.store.blobs import BlobStore
from core.store.db import Database
from core.store.observations import Observation, ObservationStore


@dataclass
class Diff:
    """What changed between two extractor versions over the same captures."""

    old_ver: str
    new_ver: str
    captures: int
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0

    @property
    def total_old(self) -> int:
        return self.unchanged + len(self.removed) + len(self.changed)

    @property
    def total_new(self) -> int:
        return self.unchanged + len(self.added) + len(self.changed)

    def summary(self) -> str:
        return (
            f"{self.old_ver} -> {self.new_ver} over {self.captures} capture(s): "
            f"{len(self.added)} added, {len(self.removed)} removed, "
            f"{len(self.changed)} changed, {self.unchanged} identical"
        )

    def is_suspicious(self) -> list[str]:
        """Cheap heuristics for 'this backfill is about to make things worse'."""
        warnings = []
        if self.total_old and len(self.removed) / max(self.total_old, 1) > 0.05:
            warnings.append(
                f"{len(self.removed)} rows the old version found are now missing "
                f"({len(self.removed) / self.total_old:.0%} of the old output). "
                "A better extractor rarely finds less."
            )
        if self.total_old and self.total_new > self.total_old * 3:
            warnings.append(
                f"row count tripled ({self.total_old} -> {self.total_new}); check for "
                "duplicate emission before committing this."
            )
        if not self.added and not self.changed and not self.removed:
            warnings.append(
                "output is byte-identical to the old version. If the extractor really "
                "changed behaviour, the version bump is wrong or the sample missed it."
            )
        return warnings


def sample_captures(
    db: Database,
    slug: str,
    n: int = 20,
    *,
    since: date | None = None,
    until: date | None = None,
    seed: int | None = 20260101,
) -> list[str]:
    """Stratified sample of capture ids: spread across time, not clustered.

    Clustered samples are how a backfill passes review and then breaks on the three
    months where the site used a different layout.
    """
    sql = "SELECT capture_id, target_date FROM captures WHERE source_slug = ?"
    params: list[Any] = [slug]
    if since:
        sql += " AND target_date >= ?"
        params.append(since)
    if until:
        sql += " AND target_date <= ?"
        params.append(until)
    sql += " ORDER BY captured_at"
    rows = db.query(sql, params)
    if len(rows) <= n:
        return [r["capture_id"] for r in rows]

    buckets: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        d = r["target_date"] or ""
        buckets[str(d)[:7] or "unknown"].append(r["capture_id"])

    rng = random.Random(seed)
    picked: list[str] = []
    months = sorted(buckets)
    per_bucket = max(1, n // max(len(months), 1))
    for month in months:
        picked += rng.sample(buckets[month], min(per_bucket, len(buckets[month])))
    if len(picked) < n:
        rest = [r["capture_id"] for r in rows if r["capture_id"] not in set(picked)]
        picked += rng.sample(rest, min(n - len(picked), len(rest)))
    return picked[:n]


def extract_dry(
    db: Database, slug: str, capture_ids: list[str], *, blobs: BlobStore | None = None
) -> list[Observation]:
    """Run the current extractor over captures without writing anything."""
    from core.scheduler.runner import Runner

    runner = Runner(db, blobs)
    module = load_extractor(slug)
    out: list[Observation] = []
    for cid in capture_ids:
        cap = runner.blobs.get_capture(cid)
        if cap is None:
            continue
        raw = runner.blobs.read(cap["sha256"])
        out += list(module.extract(raw, runner._meta_for(cap, slug)))
    return out


def diff_against_stored(
    db: Database,
    slug: str,
    capture_ids: list[str],
    *,
    old_ver: str | None = None,
    blobs: BlobStore | None = None,
) -> Diff:
    """Compare what the current extractor produces against what is already stored."""
    module = load_extractor(slug)
    new_ver = module.EXTRACTOR_VER
    store = ObservationStore(db)

    if old_ver is None:
        rows = db.query(
            "SELECT DISTINCT extractor_ver FROM observations o JOIN captures c"
            " ON c.capture_id = o.capture_id WHERE c.source_slug = ?",
            (slug,),
        )
        others = [r["extractor_ver"] for r in rows if r["extractor_ver"] != new_ver]
        old_ver = sorted(others)[-1] if others else new_ver

    wanted = set(capture_ids)
    stored: dict[tuple, Any] = {}
    for row in store.query(extractor_ver=old_ver):
        if row["capture_id"] in wanted:
            stored[(row["entity_id"], row["metric"], row["observed_at"])] = row["value"]

    produced: dict[tuple, Any] = {}
    for obs in extract_dry(db, slug, capture_ids, blobs=blobs):
        produced[(obs.entity_id, obs.metric, obs.observed_at)] = obs.value

    diff = Diff(old_ver=old_ver, new_ver=new_ver, captures=len(capture_ids))
    for key, val in produced.items():
        if key not in stored:
            diff.added.append({"key": _k(key), "new": val})
        elif stored[key] != val:
            diff.changed.append({"key": _k(key), "old": stored[key], "new": val})
        else:
            diff.unchanged += 1
    for key, val in stored.items():
        if key not in produced:
            diff.removed.append({"key": _k(key), "old": val})
    return diff


def run_backfill(
    db: Database,
    slug: str,
    *,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
    blobs: BlobStore | None = None,
) -> dict[str, Any]:
    """Commit the backfill: re-extract every archived capture at the current version.

    New rows land *alongside* the old ones. Nothing is overwritten, so both versions
    stay queryable and a later audit can compare them.
    """
    from core.scheduler.gaps import unextracted_captures
    from core.scheduler.runner import Runner

    runner = Runner(db, blobs)
    module = load_extractor(slug)
    version = module.EXTRACTOR_VER
    todo = unextracted_captures(db, slug, version)
    if since or until:
        keep = {r["capture_id"] for r in BlobStore(db).captures(slug, since=since, until=until)}
        todo = [c for c in todo if c in keep]
    if limit:
        todo = todo[:limit]

    result = runner.extract(slug, capture_ids=todo) if todo else None
    return {
        "slug": slug,
        "extractor_ver": version,
        "captures_processed": len(todo),
        "rows_added": result.row_count if result else 0,
        "status": result.status if result else "nothing-to-do",
        "error": result.error if result else None,
    }


def _k(key: tuple) -> str:
    entity, metric, observed = key
    return f"{entity} {metric} @ {observed}"
