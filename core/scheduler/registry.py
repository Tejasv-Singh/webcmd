"""Source registry and the adapter / extractor / canary loading contracts.

A source is a directory under `sources/<slug>/`, and the pieces are loaded by path
rather than imported as packages so that adding a source never means touching a
central import list -- one more reason a new source cannot break an existing one.

The contracts, in full:

    sources/<slug>/adapter/adapter.py
        SOURCE_SLUG: str
        def fetch(ctx: CaptureContext, target_date: date) -> None
            # calls ctx.get(url) and ctx.put(raw, ...). Returns nothing useful.
            # MUST NOT parse. MUST NOT produce Observations.

    sources/<slug>/extractor/extractor.py
        EXTRACTOR_VER: str
        def extract(raw_bytes: bytes, meta: dict) -> list[Observation]
            # pure. no network, no browser, no clock beyond meta.

    sources/<slug>/canary/canary.py
        def run(ctx: CanaryContext) -> list[CanaryResult]
        DRIFT: dict  (optional per-source overrides of the drift thresholds)
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

from core.config import REPO_ROOT
from core.store.db import Database
from core.timeutil import utcnow

SOURCES_DIR = REPO_ROOT / "sources"


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSpec:
    slug: str
    display_name: str
    url: str
    ring: int
    cadence: str
    calendar: str
    enabled: bool
    compliance_verdict: str | None
    notes: str | None = None

    @property
    def dir(self) -> Path:
        return SOURCES_DIR / self.slug


_MODULE_CACHE: dict[str, tuple[float, ModuleType]] = {}


def _load_module(path: Path, name: str) -> ModuleType:
    """Load a source module by path, cached on mtime.

    Cached rather than re-executed per call for two reasons. A long backfill would
    otherwise re-exec the extractor once per capture, and -- more subtly -- callers
    that hold a reference to the module (the foundation verifier bumping
    EXTRACTOR_VER, a test patching a fixture) would silently be talking to a different
    object than the runner is. Editing the file on disk still picks up the new
    version, because the mtime changes.
    """
    mtime = path.stat().st_mtime
    cached = _MODULE_CACHE.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SourceError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[name] = (mtime, module)
    return module


def clear_module_cache() -> None:
    _MODULE_CACHE.clear()


def load_adapter(slug: str) -> ModuleType:
    path = SOURCES_DIR / slug / "adapter" / "adapter.py"
    if not path.exists():
        raise SourceError(f"no adapter for {slug} at {path}")
    module = _load_module(path, f"kosh_adapter_{slug.replace('-', '_')}")
    if not hasattr(module, "fetch"):
        raise SourceError(f"{path} does not define fetch(ctx, target_date)")
    return module


def load_extractor(slug: str) -> ModuleType:
    path = SOURCES_DIR / slug / "extractor" / "extractor.py"
    if not path.exists():
        raise SourceError(f"no extractor for {slug} at {path}")
    module = _load_module(path, f"kosh_extractor_{slug.replace('-', '_')}")
    for attr in ("extract", "EXTRACTOR_VER"):
        if not hasattr(module, attr):
            raise SourceError(f"{path} does not define {attr}")
    return module


def load_canary(slug: str) -> ModuleType | None:
    path = SOURCES_DIR / slug / "canary" / "canary.py"
    if not path.exists():
        return None
    return _load_module(path, f"kosh_canary_{slug.replace('-', '_')}")


def compliance_verdict(slug: str) -> str | None:
    """Read the verdict line out of sources/<slug>/COMPLIANCE.md.

    CLAUDE.md: a source is not touched until this file exists and says PROCEED. That
    is enforced here rather than remembered, because 'I'll write the compliance doc
    after' is how every one of these projects gets into trouble.
    """
    path = SOURCES_DIR / slug / "COMPLIANCE.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip().lstrip("#*- ").upper()
        if stripped.startswith("VERDICT"):
            for token in ("PROCEED-WITH-LIMITS", "PROCEED WITH LIMITS", "PROCEED", "STOP"):
                if token in stripped:
                    return token.replace(" ", "-")
    return None


def assert_may_collect(slug: str) -> str:
    verdict = compliance_verdict(slug)
    if verdict is None:
        raise SourceError(
            f"sources/{slug}/COMPLIANCE.md is missing or has no VERDICT line. "
            "No source is touched before compliance review (CLAUDE.md)."
        )
    if verdict == "STOP":
        raise SourceError(f"compliance verdict for {slug} is STOP. Collection refused.")
    return verdict


class SourceRegistry:
    def __init__(self, db: Database) -> None:
        self.db = db

    def register(
        self,
        *,
        slug: str,
        display_name: str,
        url: str,
        ring: int = 1,
        cadence: str = "daily",
        calendar: str = "weekday",
        enabled: bool = False,
        notes: str | None = None,
    ) -> SourceSpec:
        verdict = compliance_verdict(slug)
        now = utcnow()
        if self.db.one("SELECT slug FROM sources WHERE slug = ?", (slug,)):
            self.db.execute(
                "UPDATE sources SET display_name = ?, url = ?, ring = ?, cadence = ?,"
                " calendar = ?, enabled = ?, compliance_verdict = ?, notes = ?"
                " WHERE slug = ?",
                (display_name, url, ring, cadence, calendar, enabled, verdict, notes, slug),
            )
        else:
            self.db.execute(
                "INSERT INTO sources (slug, display_name, url, ring, cadence, calendar,"
                " enabled, compliance_verdict, registered_at, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    slug,
                    display_name,
                    url,
                    ring,
                    cadence,
                    calendar,
                    enabled,
                    verdict,
                    now,
                    notes,
                ),
            )
        return self.get(slug)  # type: ignore[return-value]

    def get(self, slug: str) -> SourceSpec | None:
        row = self.db.one("SELECT * FROM sources WHERE slug = ?", (slug,))
        return _spec(row) if row else None

    def all(self, enabled_only: bool = False) -> list[SourceSpec]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1" if self.db.dialect == "sqlite" else " WHERE enabled"
        sql += " ORDER BY ring, slug"
        return [_spec(r) for r in self.db.query(sql)]

    def set_enabled(self, slug: str, enabled: bool) -> None:
        self.db.execute("UPDATE sources SET enabled = ? WHERE slug = ?", (enabled, slug))

    def discover(self) -> list[str]:
        """Slugs present on disk. A source directory with no registry row is a source
        someone built and forgot to schedule -- `kosh health` reports it."""
        if not SOURCES_DIR.exists():
            return []
        return sorted(
            p.name
            for p in SOURCES_DIR.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_"))
        )


def _spec(row: dict[str, Any]) -> SourceSpec:
    return SourceSpec(
        slug=row["slug"],
        display_name=row["display_name"],
        url=row["url"],
        ring=int(row["ring"]),
        cadence=row["cadence"],
        calendar=row["calendar"],
        enabled=bool(row["enabled"]),
        compliance_verdict=row.get("compliance_verdict"),
        notes=row.get("notes"),
    )


def parse_date_arg(value: str | date | None) -> date | None:
    """'today', 'yesterday', or an ISO date -- all IST, because the collection day is
    an Indian calendar day."""
    from core.timeutil import ist_today

    if value is None or isinstance(value, date):
        return value
    v = value.strip().lower()
    if v in ("", "none"):
        return None
    if v == "today":
        return ist_today()
    if v == "yesterday":
        from datetime import timedelta

        return ist_today() - timedelta(days=1)
    return date.fromisoformat(v)
