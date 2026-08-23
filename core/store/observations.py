"""The append-only observation store.

This is the asset. Rows go in; they never change and never leave. A correction is a
new row with a later captured_at, which is what lets us answer "what did Kosh believe
on 12 March" honestly instead of retroactively looking clever.

The store also refuses to write an Observation that is not traceable to a capture.
An observation without a capture_id cannot be re-derived, re-audited, or explained,
which makes it a rumour rather than data.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from core.store.db import AppendOnlyViolation, Database
from core.timeutil import parse_ts, utcnow

__all__ = ["AppendOnlyViolation", "Observation", "ObservationStore"]


@dataclass
class Observation:
    """A single fact, as defined in CLAUDE.md.

    observed_at is when the fact was true; captured_at is when we saw it. Conflating
    them is the most common way a market-data corpus develops lookahead bias.
    """

    entity_id: str
    metric: str
    value: Any
    observed_at: datetime
    source_url: str
    capture_id: str
    extractor_ver: str
    captured_at: datetime | None = None
    confidence: float = 1.0
    observation_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError("entity_id is required; unresolved names go to the review queue")
        if not self.metric:
            raise ValueError("metric is required")
        if not self.capture_id:
            raise ValueError(
                f"{self.metric} for {self.entity_id} has no capture_id: an observation "
                "that cannot be traced to raw bytes is not admissible"
            )
        if not self.extractor_ver:
            raise ValueError("extractor_ver is required so the row can be re-derived")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware (market data is IST-native)")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence}")

    @property
    def value_num(self) -> float | None:
        if isinstance(self.value, bool):
            return None
        return float(self.value) if isinstance(self.value, (int, float)) else None


class ObservationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- writing -------------------------------------------------------------

    def append(self, obs: Observation) -> bool:
        """Append one Observation. Returns False if this exact row already exists."""
        return self.append_many([obs]) == 1

    def append_many(self, observations: list[Observation]) -> int:
        """Append a batch. Idempotent: re-running the same extractor version over the
        same capture inserts nothing new, while a *bumped* version inserts alongside.
        That difference is the entire point of the raw/extract split.
        """
        if not observations:
            return 0
        now = utcnow()
        inserted = 0
        for obs in observations:
            captured_at = obs.captured_at or self._capture_time(obs.capture_id) or now
            exists = self.db.scalar(
                "SELECT 1 FROM observations WHERE capture_id = ? AND entity_id = ?"
                " AND metric = ? AND observed_at = ? AND extractor_ver = ?",
                (obs.capture_id, obs.entity_id, obs.metric, obs.observed_at, obs.extractor_ver),
            )
            if exists:
                continue
            self.db.execute(
                "INSERT INTO observations (observation_id, entity_id, metric, value_json,"
                " value_num, observed_at, captured_at, source_url, capture_id,"
                " extractor_ver, confidence, inserted_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs.observation_id,
                    obs.entity_id,
                    obs.metric,
                    json.dumps(obs.value, sort_keys=True, default=str),
                    obs.value_num,
                    obs.observed_at,
                    captured_at,
                    obs.source_url,
                    obs.capture_id,
                    obs.extractor_ver,
                    obs.confidence,
                    now,
                ),
            )
            inserted += 1
        return inserted

    def _capture_time(self, capture_id: str) -> datetime | None:
        row = self.db.one(
            "SELECT captured_at FROM captures WHERE capture_id = ?", (capture_id,)
        )
        return parse_ts(row["captured_at"]) if row else None

    # -- reading -------------------------------------------------------------

    def query(
        self,
        *,
        entity_id: str | None = None,
        metric: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        extractor_ver: str | None = None,
        known_at: datetime | None = None,
        latest_version_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read observations.

        `known_at` is the point-in-time filter: it restricts to rows Kosh had actually
        captured by that instant, which is what an honest backtest reads from.
        """
        sql = "SELECT * FROM observations WHERE 1 = 1"
        params: list[Any] = []
        if entity_id:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        if metric:
            sql += " AND metric = ?"
            params.append(metric)
        if since:
            sql += " AND observed_at >= ?"
            params.append(since)
        if until:
            sql += " AND observed_at < ?"
            params.append(until)
        if extractor_ver:
            sql += " AND extractor_ver = ?"
            params.append(extractor_ver)
        if known_at:
            sql += " AND captured_at <= ?"
            params.append(known_at)
        sql += " ORDER BY observed_at, entity_id, metric, captured_at, extractor_ver"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = [_normalise(r) for r in self.db.query(sql, params)]
        if latest_version_only:
            rows = _keep_best_version(rows)
        return rows

    def current(
        self,
        entity_id: str,
        metric: str,
        *,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Best current answer: newest observed_at, then newest capture, then newest
        extractor version. Corrections win because they were captured later."""
        rows = self.query(entity_id=entity_id, metric=metric, until=as_of, known_at=known_at)
        if not rows:
            return None
        rows.sort(
            key=lambda r: (
                r["observed_at"],
                r["captured_at"],
                _ver_key(r["extractor_ver"]),
            )
        )
        return rows[-1]

    def history(self, entity_id: str, metric: str) -> list[dict[str, Any]]:
        """Every row we ever wrote for this fact, corrections included."""
        return self.query(entity_id=entity_id, metric=metric)

    def versions(self, metric: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT extractor_ver, COUNT(*) AS n FROM observations"
        params: list[Any] = []
        if metric:
            sql += " WHERE metric = ?"
            params.append(metric)
        sql += " GROUP BY extractor_ver ORDER BY extractor_ver"
        return self.db.query(sql, params)

    def count(self, **kwargs: Any) -> int:
        return len(self.query(**kwargs))

    def counts_by_day(self, source_slug: str, days: int = 30) -> list[dict[str, Any]]:
        """Rows per collection day for a source -- the input to drift detection."""
        return self.db.query(
            "SELECT c.target_date AS target_date, COUNT(*) AS n"
            " FROM observations o JOIN captures c ON c.capture_id = o.capture_id"
            " WHERE c.source_slug = ? AND c.target_date IS NOT NULL"
            " GROUP BY c.target_date ORDER BY c.target_date DESC"
            f" LIMIT {int(days)}",
            (source_slug,),
        )


def _ver_key(ver: str) -> tuple:
    """Sort extractor versions numerically where they look numeric ('v2' > 'v10' is
    the classic bug this avoids)."""
    parts = []
    for chunk in str(ver).replace("v", "").replace("-", ".").split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)


def _keep_best_version(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r["entity_id"], r["metric"], r["observed_at"], r["capture_id"])
        cur = best.get(key)
        if cur is None or _ver_key(r["extractor_ver"]) > _ver_key(cur["extractor_ver"]):
            best[key] = r
    return sorted(best.values(), key=lambda r: (r["observed_at"], r["entity_id"], r["metric"]))


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for col in ("observed_at", "captured_at", "inserted_at"):
        if col in out:
            out[col] = parse_ts(out[col])
    raw = out.get("value_json")
    out["value"] = json.loads(raw) if isinstance(raw, str) else raw
    return out


def latest_captured_at(db: Database, source_slug: str) -> datetime | None:
    row = db.one(
        "SELECT MAX(o.captured_at) AS t FROM observations o"
        " JOIN captures c ON c.capture_id = o.capture_id WHERE c.source_slug = ?",
        (source_slug,),
    )
    return parse_ts(row["t"]) if row and row.get("t") else None


def observed_dates(db: Database, source_slug: str) -> list[date]:
    rows = db.query(
        "SELECT DISTINCT c.target_date AS d FROM observations o"
        " JOIN captures c ON c.capture_id = o.capture_id"
        " WHERE c.source_slug = ? AND c.target_date IS NOT NULL ORDER BY d",
        (source_slug,),
    )
    out = []
    for r in rows:
        d = r["d"]
        out.append(date.fromisoformat(d) if isinstance(d, str) else d)
    return out
