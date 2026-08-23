"""amfi-nav-probe extractor — raw bytes to Observations.

A pure function of (raw_bytes, meta). No network, no browser, no clock: everything it
needs is in the bytes or in meta, which is what makes it runnable over a blob captured
ten months ago and testable with no infrastructure at all.

Bump EXTRACTOR_VER whenever the output changes for the same input. That is the whole
contract — the store keys on it, and `kosh backfill` uses it to decide what still
needs re-extracting.

## Why this parses by header rather than by position

v1 read fields at fixed indices, from a hand-written brief describing a six-column
layout. The live file has eight: AMFI added `Plan` and `Option` between the scheme
name and the NAV. v1 therefore read `Plan` as the NAV, failed to parse it, and
returned **zero observations from a perfectly good 1.5 MB capture** — the exact silent
failure CLAUDE.md rule 4 is about, and it cost nothing to find only because the raw
bytes were archived first and could be re-read.

v2 locates columns by header name and falls back to the historic layout when a file
predates the header row. Both old and new captures now extract correctly, which is the
whole point of keeping raw.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from core.entities.graph import entity_id_for, is_isin
from core.store.observations import Observation
from core.timeutil import ist_date_to_utc

EXTRACTOR_VER = "amfi-nav/v2"

# AMFI publishes NAVs after the close; the value is the day's closing NAV, so it is
# stamped at the end of the IST business day rather than at midnight.
NAV_STAMP = time(18, 0)

_NULL_VALUES = {"", "-", "n.a.", "na", "n/a", "null", "none"}

# Header fragments -> our field names. Matched on a normalised, lowercased header cell,
# so "ISIN Div Payout/ ISIN Growth" and "ISIN Div Payout/ISIN Growth" both land.
_HEADER_MATCHERS = (
    ("scheme_code", ("scheme code",)),
    ("isin", ("isin div payout", "isin growth")),
    ("isin_reinvest", ("isin div reinvestment",)),
    ("scheme_name", ("scheme name",)),
    ("plan", ("plan",)),
    ("option", ("option",)),
    ("nav", ("net asset value", "nav")),
    ("nav_date", ("date",)),
)

# The layout to assume for a file with no recognisable header row (the six-column
# form this source used historically). Documented in BRIEF.md.
_LEGACY_LAYOUT = {
    "scheme_code": 0,
    "isin": 1,
    "isin_reinvest": 2,
    "scheme_name": 3,
    "nav": 4,
    "nav_date": 5,
}


def extract(raw_bytes: bytes, meta: dict[str, Any]) -> list[Observation]:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    layout = _detect_layout(lines)

    out: list[Observation] = []
    seen: set[tuple[str, date]] = set()

    for line in lines:
        line = line.strip()
        if not line or ";" not in line:
            # Category headings, fund-house names and separator lines carry no
            # semicolons. That is the only structural signal the file gives us.
            continue

        parts = [p.strip() for p in line.split(";")]
        if _is_header(parts):
            continue

        row = _row(parts, layout)
        if row is None:
            continue

        isin = row["isin"].upper()
        if not is_isin(isin):
            # No ISIN means no key. Joining on the scheme name instead would be a
            # display-name join wearing a disguise (CLAUDE.md rule 3).
            continue

        nav = _parse_nav(row["nav"])
        nav_date = _parse_date(row["nav_date"])
        if nav is None or nav_date is None:
            # 'N.A.' is a scheme that did not price today, not a failure. Skipping is
            # correct; writing a zero would be a fabricated observation.
            continue

        key = (isin, nav_date)
        if key in seen:
            continue
        seen.add(key)

        common = {
            "observed_at": ist_date_to_utc(nav_date, NAV_STAMP),
            "source_url": meta["source_url"],
            "capture_id": meta["capture_id"],
            "extractor_ver": EXTRACTOR_VER,
            "captured_at": meta.get("captured_at"),
        }
        entity_id = entity_id_for(isin)

        out.append(
            Observation(
                entity_id=entity_id, metric="nav.close", value=nav, confidence=1.0, **common
            )
        )
        # The name is recorded as an observation rather than written into the entity
        # graph: it is a fact the source asserted on a date, and scheme names change.
        descriptor = {
            "name": row["scheme_name"],
            "amfi_code": row["scheme_code"],
        }
        if row.get("plan"):
            descriptor["plan"] = row["plan"]
        if row.get("option"):
            descriptor["option"] = row["option"]
        out.append(
            Observation(
                entity_id=entity_id,
                metric="fund.scheme_name",
                value=descriptor,
                confidence=1.0,
                **common,
            )
        )
    return out


def _detect_layout(lines: list[str]) -> dict[str, int]:
    """Find each field's column index from the header row.

    Falls back to the historic six-column layout, so blobs captured before AMFI added
    the Plan and Option columns still extract.
    """
    for line in lines[:20]:
        parts = [p.strip() for p in line.split(";")]
        if not _is_header(parts):
            continue
        layout: dict[str, int] = {}
        for field, fragments in _HEADER_MATCHERS:
            for i, cell in enumerate(parts):
                if i in layout.values():
                    continue
                if any(f in _norm_header(cell) for f in fragments):
                    layout[field] = i
                    break
        if {"isin", "nav", "nav_date"} <= set(layout):
            return layout
    return dict(_LEGACY_LAYOUT)


def _norm_header(cell: str) -> str:
    """Collapse whitespace and case so 'ISIN Div Payout/ ISIN Growth' and
    'ISIN Div Payout/ISIN Growth' normalise to the same thing."""
    return " ".join(cell.lower().replace("/", "/ ").split())


def _is_header(parts: list[str]) -> bool:
    """A line is the header if several of its cells are recognisable column names.

    Deliberately not "starts with Scheme Code": the column *order* is no more
    guaranteed than the column count, and the whole point of v2 is to stop trusting
    position. A data row scores at most one or two hits (a scheme name containing the
    word "Plan"), so the threshold has comfortable margin.
    """
    if len(parts) < 2:
        return False
    hits = 0
    for cell in parts:
        norm = _norm_header(cell)
        if any(any(f in norm for f in fragments) for _, fragments in _HEADER_MATCHERS):
            hits += 1
    return hits >= 4


def _row(parts: list[str], layout: dict[str, int]) -> dict[str, str] | None:
    needed = max(layout[f] for f in ("isin", "nav", "nav_date"))
    if len(parts) <= needed:
        return None
    return {field: parts[i] if i < len(parts) else "" for field, i in layout.items()}


def _parse_nav(raw: str) -> float | None:
    if raw.strip().lower() in _NULL_VALUES:
        return None
    try:
        nav = float(raw.replace(",", ""))
    except ValueError:
        return None
    # A NAV of zero or below is not a real price; it is a parse that went wrong.
    return nav if nav > 0 else None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
