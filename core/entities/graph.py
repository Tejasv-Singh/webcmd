"""The ISIN-keyed entity graph (CLAUDE.md rule 3).

Infosys is INFY on NSE, 500209 on BSE, INE009A01021 by ISIN, "Infosys Limited" in a
PDF header and "Infosys Ltd." in the footer of the same PDF. Every one of those has to
land on one entity_id, and that entity_id is the ISIN wherever an ISIN exists.

Two rules do most of the work here:

1. **Never join on a display name.** Names are resolved through the alias table, which
   is date-scoped, because names change and a corpus that forgets that produces
   backtests that quietly lie.
2. **Defer rather than guess.** An unrecognised or ambiguous alias goes to the review
   queue and returns None. A wrong join is worse than a missing one -- a gap is
   visible, a bad join silently contaminates every query downstream.
"""

from __future__ import annotations

import csv
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from core.store.db import Database
from core.timeutil import ist_today, utcnow

ALIAS_KINDS = (
    "isin",
    "nse_symbol",
    "bse_code",
    "amfi_code",
    "legal_name",
    "display_name",
)

ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")

# Company-suffix noise. Stripped for matching only -- primary_name keeps the real name.
_SUFFIXES = (
    "LIMITED",
    "LTD",
    "PRIVATE",
    "PVT",
    "PUBLIC",
    "COMPANY",
    "CO",
    "CORPORATION",
    "CORP",
    "INCORPORATED",
    "INC",
    "PLC",
    "THE",
)
_NAME_NOISE = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def is_isin(value: str) -> bool:
    return bool(ISIN_RE.match(value.strip().upper()))


def normalise(kind: str, raw: str) -> str:
    """Fold an alias to its comparison form.

    Aggressive on names, conservative on codes: 'INFY ' and 'infy' are the same symbol,
    but '500209' and '500209.0' are not the same BSE code by accident -- that is a
    spreadsheet artefact and should be caught, not silently absorbed.
    """
    v = (raw or "").strip()
    if not v:
        return ""
    if kind in ("legal_name", "display_name"):
        v = _NAME_NOISE.sub(" ", v.upper())
        words = [w for w in _WS.sub(" ", v).strip().split(" ") if w]
        while words and words[-1] in _SUFFIXES:
            words.pop()
        words = [w for w in words if w != "THE"]
        return " ".join(words)
    return v.upper().strip()


def entity_id_for(isin: str | None, fallback_name: str | None = None) -> str:
    """ISIN-keyed where possible; ORG-keyed where an ISIN genuinely does not exist
    (a private company whose careers page we watch, say)."""
    if isin and is_isin(isin):
        return f"ISIN:{isin.strip().upper()}"
    if not fallback_name:
        raise ValueError("need an ISIN or a name to mint an entity_id")
    slug = re.sub(r"[^a-z0-9]+", "-", fallback_name.strip().lower()).strip("-")
    return f"ORG:{slug}"


@dataclass(frozen=True)
class Resolution:
    entity_id: str
    matched_kind: str
    confidence: float
    method: str


class EntityGraph:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- writing -------------------------------------------------------------

    def upsert_entity(
        self,
        *,
        isin: str | None,
        primary_name: str,
        entity_type: str = "equity",
        status: str = "active",
    ) -> str:
        eid = entity_id_for(isin, primary_name)
        now = utcnow()
        existing = self.db.one("SELECT entity_id FROM entities WHERE entity_id = ?", (eid,))
        if existing:
            self.db.execute(
                "UPDATE entities SET primary_name = ?, status = ?, updated_at = ?"
                " WHERE entity_id = ?",
                (primary_name, status, now, eid),
            )
        else:
            self.db.execute(
                "INSERT INTO entities (entity_id, isin, primary_name, entity_type, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    eid,
                    isin.strip().upper() if isin and is_isin(isin) else None,
                    primary_name,
                    entity_type,
                    status,
                    now,
                    now,
                ),
            )
        if isin and is_isin(isin):
            self.add_alias(eid, "isin", isin, source="upsert")
        self.add_alias(eid, "legal_name", primary_name, source="upsert")
        return eid

    def add_alias(
        self,
        entity_id: str,
        kind: str,
        raw: str,
        *,
        valid_from: date | None = None,
        valid_to: date | None = None,
        source: str | None = None,
        confidence: float = 1.0,
    ) -> bool:
        """Add an alias. Returns False if this exact alias window already exists.

        Raises on a collision: the same symbol pointing at two entities in the same
        window is a real-world identity event (a symbol reassigned after a delisting),
        and it must be modelled explicitly rather than absorbed.
        """
        if kind not in ALIAS_KINDS:
            raise ValueError(f"unknown alias kind {kind!r}; add it to ALIAS_KINDS deliberately")
        norm = normalise(kind, raw)
        if not norm:
            return False
        vf = valid_from or date(1900, 1, 1)
        row = self.db.one(
            "SELECT entity_id FROM entity_aliases WHERE alias_kind = ? AND alias_norm = ?"
            " AND valid_from = ?",
            (kind, norm, vf),
        )
        if row:
            if row["entity_id"] != entity_id:
                raise ValueError(
                    f"alias collision: {kind}={norm!r} from {vf} already points at "
                    f"{row['entity_id']}, not {entity_id}. Close the old window with an "
                    "identity event instead of overwriting it."
                )
            return False
        self.db.execute(
            "INSERT INTO entity_aliases (entity_id, alias_kind, alias_norm, alias_raw,"
            " valid_from, valid_to, source, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_id, kind, norm, raw, vf, valid_to, source, confidence, utcnow()),
        )
        return True

    def close_alias(self, kind: str, raw: str, *, valid_to: date) -> int:
        """End an alias window. The row stays; only its end date is set."""
        cur = self.db.execute(
            "UPDATE entity_aliases SET valid_to = ? WHERE alias_kind = ? AND alias_norm = ?"
            " AND valid_to IS NULL",
            (valid_to, kind, normalise(kind, raw)),
        )
        return cur.rowcount or 0

    # -- resolution ----------------------------------------------------------

    def resolve(
        self,
        raw: str,
        *,
        kind: str | None = None,
        as_of: date | None = None,
    ) -> Resolution | None:
        """Resolve an alias to an entity_id, or None. Never guesses."""
        raw = (raw or "").strip()
        if not raw:
            return None
        as_of = as_of or ist_today()

        if is_isin(raw):
            hit = self._lookup("isin", raw, as_of)
            if hit:
                return Resolution(hit, "isin", 1.0, "exact-isin")

        kinds = [kind] if kind else ["nse_symbol", "bse_code", "amfi_code", "isin"]
        for k in kinds:
            hit = self._lookup(k, raw, as_of)
            if hit:
                return Resolution(hit, k, 1.0, "exact-code")

        if kind in (None, "legal_name", "display_name"):
            for k in ("legal_name", "display_name"):
                hit = self._lookup(k, raw, as_of)
                if hit:
                    # A name match is a real match, but it is the weakest kind we accept
                    # and the confidence should say so downstream.
                    return Resolution(hit, k, 0.9, "exact-normalised-name")
        return None

    def _lookup(self, kind: str, raw: str, as_of: date) -> str | None:
        norm = normalise(kind, raw)
        if not norm:
            return None
        rows = self.db.query(
            "SELECT DISTINCT entity_id FROM entity_aliases"
            " WHERE alias_kind = ? AND alias_norm = ? AND valid_from <= ?"
            " AND (valid_to IS NULL OR valid_to > ?)",
            (kind, norm, as_of, as_of),
        )
        if len(rows) == 1:
            return rows[0]["entity_id"]
        return None  # zero matches, or ambiguous -- either way, defer

    def resolve_or_queue(
        self,
        raw: str,
        *,
        kind: str,
        source_slug: str | None = None,
        as_of: date | None = None,
        context: dict[str, Any] | None = None,
    ) -> str | None:
        """The call site an extractor should use. Returns None rather than guessing,
        and leaves a queue entry so the miss is visible instead of silent."""
        hit = self.resolve(raw, kind=kind, as_of=as_of)
        if hit:
            return hit.entity_id
        self.enqueue(raw, kind=kind, source_slug=source_slug, context=context)
        return None

    # -- review queue --------------------------------------------------------

    def enqueue(
        self,
        raw: str,
        *,
        kind: str,
        source_slug: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        norm = normalise(kind, raw)
        if not norm:
            return
        now = utcnow()
        row = self.db.one(
            "SELECT queue_id, occurrences FROM alias_review_queue"
            " WHERE alias_kind = ? AND alias_norm = ?",
            (kind, norm),
        )
        if row:
            self.db.execute(
                "UPDATE alias_review_queue SET occurrences = ?, last_seen_at = ?"
                " WHERE queue_id = ?",
                (row["occurrences"] + 1, now, row["queue_id"]),
            )
        else:
            self.db.execute(
                "INSERT INTO alias_review_queue (alias_kind, alias_norm, alias_raw,"
                " source_slug, context_json, occurrences, first_seen_at, last_seen_at,"
                " status) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'open')",
                (
                    kind,
                    norm,
                    raw,
                    source_slug,
                    json.dumps(context or {}, default=str),
                    now,
                    now,
                ),
            )

    def queue(self, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM alias_review_queue WHERE status = ?"
            f" ORDER BY occurrences DESC, first_seen_at LIMIT {int(limit)}",
            (status,),
        )

    def resolve_queue_item(self, queue_id: int, entity_id: str) -> None:
        row = self.db.one("SELECT * FROM alias_review_queue WHERE queue_id = ?", (queue_id,))
        if not row:
            raise KeyError(f"no queue item {queue_id}")
        self.add_alias(entity_id, row["alias_kind"], row["alias_raw"], source="review-queue")
        self.db.execute(
            "UPDATE alias_review_queue SET status = 'resolved', resolved_entity_id = ?"
            " WHERE queue_id = ?",
            (entity_id, queue_id),
        )

    # -- identity events -----------------------------------------------------

    def record_identity_event(
        self,
        *,
        event_type: str,
        entity_id: str,
        effective_date: date,
        related_entity_id: str | None = None,
        details: dict[str, Any] | None = None,
        source_url: str | None = None,
    ) -> str:
        """Append an identity event. These rows are append-only like observations --
        a merger that we later learn happened on a different date is a new event, not
        an edit, because our belief on the old date was a real fact about Kosh."""
        valid = {"name_change", "symbol_change", "merger", "demerger", "delisting", "relisting"}
        if event_type not in valid:
            raise ValueError(f"unknown identity event {event_type!r}; expected one of {valid}")
        event_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO identity_events (event_id, event_type, entity_id,"
            " related_entity_id, effective_date, details_json, source_url, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                event_type,
                entity_id,
                related_entity_id,
                effective_date,
                json.dumps(details or {}, default=str),
                source_url,
                utcnow(),
            ),
        )
        return event_id

    def apply_symbol_change(
        self,
        *,
        entity_id: str,
        old_symbol: str,
        new_symbol: str,
        effective_date: date,
        kind: str = "nse_symbol",
        source_url: str | None = None,
    ) -> str:
        """Close the old symbol window, open the new one, record the event.

        Both windows survive, which is what makes a 2024 filing that says the old
        symbol still resolve correctly in 2026.
        """
        self.close_alias(kind, old_symbol, valid_to=effective_date)
        self.add_alias(
            entity_id, kind, new_symbol, valid_from=effective_date, source="symbol-change"
        )
        return self.record_identity_event(
            event_type="symbol_change",
            entity_id=entity_id,
            effective_date=effective_date,
            details={"old": old_symbol, "new": new_symbol, "kind": kind},
            source_url=source_url,
        )

    def apply_delisting(
        self, *, entity_id: str, effective_date: date, source_url: str | None = None
    ) -> str:
        self.db.execute(
            "UPDATE entities SET status = 'delisted', updated_at = ? WHERE entity_id = ?",
            (utcnow(), entity_id),
        )
        for row in self.db.query(
            "SELECT alias_kind, alias_raw FROM entity_aliases"
            " WHERE entity_id = ? AND valid_to IS NULL AND alias_kind IN"
            " ('nse_symbol', 'bse_code')",
            (entity_id,),
        ):
            self.close_alias(row["alias_kind"], row["alias_raw"], valid_to=effective_date)
        return self.record_identity_event(
            event_type="delisting",
            entity_id=entity_id,
            effective_date=effective_date,
            source_url=source_url,
        )

    def events(self, entity_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM identity_events WHERE entity_id = ? ORDER BY effective_date",
            (entity_id,),
        )

    # -- reporting -----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "entities": self.db.scalar("SELECT COUNT(*) FROM entities") or 0,
            "with_isin": self.db.scalar("SELECT COUNT(*) FROM entities WHERE isin IS NOT NULL")
            or 0,
            "aliases": self.db.scalar("SELECT COUNT(*) FROM entity_aliases") or 0,
            "identity_events": self.db.scalar("SELECT COUNT(*) FROM identity_events") or 0,
            "queue_open": self.db.scalar(
                "SELECT COUNT(*) FROM alias_review_queue WHERE status = 'open'"
            )
            or 0,
        }


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_from_csv(graph: EntityGraph, path: Path, source: str = "seed") -> dict[str, int]:
    """Seed from a CSV with columns: isin, name, nse_symbol, bse_code, entity_type.

    Only isin and name are required. Rows without a valid ISIN are seeded as ORG:
    entities -- useful for Ring 2 alt-data targets that are not listed.
    """
    added = {"entities": 0, "aliases": 0, "skipped": 0}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                added["skipped"] += 1
                continue
            isin = (row.get("isin") or "").strip() or None
            eid = graph.upsert_entity(
                isin=isin,
                primary_name=name,
                entity_type=(row.get("entity_type") or "equity").strip() or "equity",
            )
            added["entities"] += 1
            for col, kind in (
                ("nse_symbol", "nse_symbol"),
                ("bse_code", "bse_code"),
                ("amfi_code", "amfi_code"),
            ):
                val = (row.get(col) or "").strip()
                if val and graph.add_alias(eid, kind, val, source=source):
                    added["aliases"] += 1
    return added


def parse_nse_equity_list(raw: bytes) -> list[dict[str, str]]:
    """Parse NSE's EQUITY_L.csv (the official 'securities available for trading' list).

    Kept as a pure function on bytes so it obeys rule 2 like any other extractor: the
    CLI fetches, this parses. Columns as published: SYMBOL, NAME OF COMPANY,
    SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    out: list[dict[str, str]] = []
    for row in csv.DictReader(text.splitlines()):
        clean = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        isin = clean.get("ISIN NUMBER", "")
        symbol = clean.get("SYMBOL", "")
        name = clean.get("NAME OF COMPANY", "")
        if not symbol or not name:
            continue
        out.append(
            {
                "isin": isin,
                "name": name,
                "nse_symbol": symbol,
                "series": clean.get("SERIES", ""),
                "listing_date": clean.get("DATE OF LISTING", ""),
            }
        )
    return out
