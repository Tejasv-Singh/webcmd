"""Content-addressed raw blob store.

Adapters do exactly one job: fetch bytes and put them here (CLAUDE.md rule 2). The
bytes are keyed by sha256, so fetching the same content twice stores one blob and two
capture records -- we keep the full fetch history without paying for it twice, and an
extractor written in month nine can be re-run over everything captured in month one.

Nothing in this module parses anything. That is deliberate and load-bearing.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core.config import settings
from core.store.db import Database
from core.timeutil import parse_ts, utcnow


@dataclass(frozen=True)
class Capture:
    """One fetch event. Many captures may point at the same blob."""

    capture_id: str
    source_slug: str
    source_url: str
    sha256: str
    captured_at: datetime
    target_date: date | None = None
    http_status: int | None = None
    run_id: str | None = None
    fetch_meta: dict[str, Any] = field(default_factory=dict)
    deduplicated: bool = False
    """True when these exact bytes were already in the store."""


class BlobStore:
    def __init__(self, db: Database, root: Path | None = None) -> None:
        self.db = db
        self.root = Path(root) if root else settings().blob_root
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------------

    @staticmethod
    def rel_path_for(sha: str) -> str:
        """Fan out two levels so no directory ends up with a million entries."""
        return f"{sha[:2]}/{sha[2:4]}/{sha}"

    def abs_path_for(self, sha: str) -> Path:
        return self.root / self.rel_path_for(sha)

    # -- writing -------------------------------------------------------------

    def put(
        self,
        raw: bytes,
        *,
        source_slug: str,
        source_url: str,
        target_date: date | None = None,
        http_status: int | None = None,
        run_id: str | None = None,
        content_type: str | None = None,
        fetch_meta: dict[str, Any] | None = None,
        captured_at: datetime | None = None,
    ) -> Capture:
        """Persist bytes and record the fetch. Idempotent on content, not on fetch.

        Writing the file before the row, and writing it via a temp file plus rename,
        is what makes a killed run safe: the worst case is an orphan blob on disk with
        no capture row, which is recoverable. The reverse -- a row pointing at bytes
        that are not there -- is not.
        """
        if not isinstance(raw, bytes):
            raise TypeError("raw capture must be bytes; adapters do not parse")
        sha = hashlib.sha256(raw).hexdigest()
        now = captured_at or utcnow()
        rel = self.rel_path_for(sha)
        path = self.root / rel

        existed = self.db.scalar("SELECT 1 FROM raw_blobs WHERE sha256 = ?", (sha,)) is not None

        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp-" + uuid.uuid4().hex[:8])
            tmp.write_bytes(raw)
            tmp.replace(path)

        if not existed:
            self.db.execute(
                "INSERT INTO raw_blobs (sha256, byte_len, content_type, rel_path,"
                " first_seen_at) VALUES (?, ?, ?, ?, ?)",
                (sha, len(raw), content_type, rel, now),
            )

        capture_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO captures (capture_id, source_slug, source_url, sha256,"
            " target_date, captured_at, http_status, run_id, fetch_meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                capture_id,
                source_slug,
                source_url,
                sha,
                target_date,
                now,
                http_status,
                run_id,
                json.dumps(fetch_meta or {}, sort_keys=True),
            ),
        )
        return Capture(
            capture_id=capture_id,
            source_slug=source_slug,
            source_url=source_url,
            sha256=sha,
            captured_at=now,
            target_date=target_date,
            http_status=http_status,
            run_id=run_id,
            fetch_meta=fetch_meta or {},
            deduplicated=existed,
        )

    # -- reading -------------------------------------------------------------

    def read(self, sha: str) -> bytes:
        path = self.abs_path_for(sha)
        if not path.exists():
            raise FileNotFoundError(
                f"blob {sha} is in the index but missing on disk at {path}. "
                "This is data loss -- do not extract around it, investigate it."
            )
        return path.read_bytes()

    def read_capture(self, capture_id: str) -> bytes:
        row = self.db.one("SELECT sha256 FROM captures WHERE capture_id = ?", (capture_id,))
        if row is None:
            raise KeyError(f"no such capture: {capture_id}")
        return self.read(row["sha256"])

    def get_capture(self, capture_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM captures WHERE capture_id = ?", (capture_id,))
        return _normalise_capture(row) if row else None

    def captures(
        self,
        source_slug: str,
        *,
        since: date | None = None,
        until: date | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM captures WHERE source_slug = ?"
        params: list[Any] = [source_slug]
        if since:
            sql += " AND target_date >= ?"
            params.append(since)
        if until:
            sql += " AND target_date <= ?"
            params.append(until)
        sql += " ORDER BY captured_at"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_normalise_capture(r) for r in self.db.query(sql, params)]

    def stats(self) -> dict[str, Any]:
        blobs = self.db.scalar("SELECT COUNT(*) FROM raw_blobs") or 0
        caps = self.db.scalar("SELECT COUNT(*) FROM captures") or 0
        total_bytes = self.db.scalar("SELECT COALESCE(SUM(byte_len), 0) FROM raw_blobs") or 0
        return {
            "blobs": blobs,
            "captures": caps,
            "bytes": int(total_bytes),
            "dedup_ratio": round(caps / blobs, 3) if blobs else 0.0,
        }

    def verify(self, limit: int | None = None) -> list[str]:
        """Re-hash stored blobs and report any that no longer match their key."""
        sql = "SELECT sha256 FROM raw_blobs ORDER BY first_seen_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        bad = []
        for row in self.db.query(sql):
            sha = row["sha256"]
            path = self.abs_path_for(sha)
            if not path.exists():
                bad.append(f"{sha}: missing on disk")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                bad.append(f"{sha}: content does not match hash")
        return bad


def _normalise_capture(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["captured_at"] = parse_ts(out.get("captured_at"))
    meta = out.get("fetch_meta")
    if isinstance(meta, str):
        out["fetch_meta"] = json.loads(meta or "{}")
    td = out.get("target_date")
    if isinstance(td, str) and td:
        out["target_date"] = date.fromisoformat(td)
    return out
