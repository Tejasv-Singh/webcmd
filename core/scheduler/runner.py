"""Executing a collection run: the capture phase and the extraction phase.

These are two separate passes on purpose (CLAUDE.md rule 2). `collect` never parses;
`extract` never fetches. The seam between them is the blob store, which is what makes
it possible to improve an extractor in month nine and re-run it over ten months of
archived captures.

Crash safety: the run row is written *before* the work and finished after, so a
killed process leaves a row in state 'running' rather than pretending nothing
happened. `stale_runs()` finds those; the store itself stays consistent because blobs
are content-addressed and observations are idempotent per (capture, version).
"""

from __future__ import annotations

import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.entities.graph import EntityGraph
from core.scheduler.fetch import Fetcher
from core.scheduler.registry import (
    SourceRegistry,
    assert_may_collect,
    load_adapter,
    load_extractor,
)
from core.store.blobs import BlobStore, Capture
from core.store.db import Database
from core.store.observations import Observation, ObservationStore
from core.timeutil import utcnow


@dataclass
class RunResult:
    run_id: str
    source_slug: str
    phase: str
    target_date: date | None
    status: str
    blob_count: int = 0
    row_count: int = 0
    new_blobs: int = 0
    error: str | None = None
    extractor_ver: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class CaptureContext:
    """What an adapter is handed. Deliberately narrow.

    There is no observation store on here, and that is the point: an adapter that
    wanted to write an Observation would have to go and find one, which is exactly the
    friction we want at that boundary.
    """

    def __init__(
        self,
        *,
        slug: str,
        blobs: BlobStore,
        run_id: str,
        target_date: date | None,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.slug = slug
        self.blobs = blobs
        self.run_id = run_id
        self.target_date = target_date
        self.fetcher = fetcher or Fetcher()
        self.captures: list[Capture] = []
        self.notes: list[str] = []

    def get(self, url: str, **kwargs: Any):
        """Polite, robots-respecting HTTP GET."""
        return self.fetcher.get(url, **kwargs)

    def put(
        self,
        raw: bytes,
        *,
        url: str,
        http_status: int | None = None,
        content_type: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Capture:
        cap = self.blobs.put(
            raw,
            source_slug=self.slug,
            source_url=url,
            target_date=self.target_date,
            http_status=http_status,
            run_id=self.run_id,
            content_type=content_type,
            fetch_meta=meta,
        )
        self.captures.append(cap)
        return cap

    def fetch_and_put(self, url: str, **kwargs: Any) -> Capture:
        """The common case, in one call."""
        resp = self.get(url, **kwargs)
        return self.put(
            resp.body,
            url=resp.url,
            http_status=resp.status,
            content_type=resp.content_type,
            meta={"request_url": url},
        )

    def note(self, message: str) -> None:
        self.notes.append(message)


class Runner:
    def __init__(
        self,
        db: Database,
        blobs: BlobStore | None = None,
        *,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        """Injectable so foundation verification can exercise a real adapter against a
        fixture instead of hitting the live source. Left None in normal operation."""
        self.blobs = blobs or BlobStore(db)
        self.obs = ObservationStore(db)
        self.registry = SourceRegistry(db)
        self.graph = EntityGraph(db)

    # -- run bookkeeping -----------------------------------------------------

    def _start(self, slug: str, phase: str, target_date: date | None) -> str:
        run_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO runs (run_id, source_slug, phase, target_date, status, started_at)"
            " VALUES (?, ?, ?, ?, 'running', ?)",
            (run_id, slug, phase, target_date, utcnow()),
        )
        self.db.commit()
        return run_id

    def _finish(self, result: RunResult) -> RunResult:
        self.db.execute(
            "UPDATE runs SET status = ?, finished_at = ?, blob_count = ?, row_count = ?,"
            " extractor_ver = ?, error = ? WHERE run_id = ?",
            (
                result.status,
                utcnow(),
                result.blob_count,
                result.row_count,
                result.extractor_ver,
                result.error,
                result.run_id,
            ),
        )
        self.db.commit()
        return result

    def stale_runs(self, older_than_minutes: int = 180) -> list[dict[str, Any]]:
        """Runs still marked 'running' long after they should have finished -- i.e.
        processes that were killed. Reported, never silently reaped."""
        rows = self.db.query("SELECT * FROM runs WHERE status = 'running' ORDER BY started_at")
        from core.timeutil import parse_ts

        cutoff = utcnow().timestamp() - older_than_minutes * 60
        out = []
        for r in rows:
            started = parse_ts(r["started_at"])
            if started and started.timestamp() < cutoff:
                out.append(r)
        return out

    # -- phase 1: capture ----------------------------------------------------

    def collect(
        self,
        slug: str,
        target_date: date | None = None,
        *,
        skip_compliance: bool = False,
    ) -> RunResult:
        """Fetch and persist raw bytes for one source on one date. Parses nothing."""
        if not skip_compliance:
            assert_may_collect(slug)
        adapter = load_adapter(slug)
        run_id = self._start(slug, "capture", target_date)
        result = RunResult(run_id, slug, "capture", target_date, "running")
        ctx = CaptureContext(
            slug=slug,
            blobs=self.blobs,
            run_id=run_id,
            target_date=target_date,
            fetcher=self.fetcher,
        )
        try:
            adapter.fetch(ctx, target_date)
            self.db.commit()
        except Exception as exc:
            self.db.commit()  # keep whatever landed before the failure
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
            result.blob_count = len(ctx.captures)
            return self._finish(result)

        result.blob_count = len(ctx.captures)
        result.new_blobs = sum(1 for c in ctx.captures if not c.deduplicated)
        result.notes = ctx.notes
        # Rule 4: an adapter that captured nothing did not "succeed quietly".
        result.status = "ok" if ctx.captures else "empty"
        if not ctx.captures:
            result.error = (
                "adapter captured zero blobs. Per CLAUDE.md rule 4 this is an incident, "
                "not an empty result -- check the source before assuming a quiet day."
            )
        return self._finish(result)

    # -- phase 2: extraction -------------------------------------------------

    def extract(
        self,
        slug: str,
        target_date: date | None = None,
        *,
        capture_ids: list[str] | None = None,
        since: date | None = None,
        until: date | None = None,
    ) -> RunResult:
        """Turn stored captures into Observations. Touches no network."""
        module = load_extractor(slug)
        version = module.EXTRACTOR_VER
        run_id = self._start(slug, "extract", target_date)
        result = RunResult(
            run_id, slug, "extract", target_date, "running", extractor_ver=version
        )

        if capture_ids:
            captures = [c for c in (self.blobs.get_capture(cid) for cid in capture_ids) if c]
        else:
            captures = self.blobs.captures(
                slug, since=since or target_date, until=until or target_date
            )
        result.blob_count = len(captures)

        try:
            total = 0
            unresolved = 0
            for cap in captures:
                raw = self.blobs.read(cap["sha256"])
                meta = self._meta_for(cap, slug)
                observations = module.extract(raw, meta)
                _validate(observations, cap, version)
                total += self.obs.append_many(list(observations))
                unresolved += meta["_unresolved"][0]
            self.db.commit()
            result.row_count = total
            if unresolved:
                result.notes.append(
                    f"{unresolved} alias(es) went to the review queue unresolved"
                )
            result.status = "ok" if total or not captures else "empty"
            if captures and not total:
                result.error = (
                    "extraction produced zero observations from non-empty captures. "
                    "Either the page changed shape or the extractor is broken -- both "
                    "are P0."
                )
        except Exception as exc:
            self.db.commit()
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
        return self._finish(result)

    def _meta_for(self, cap: dict[str, Any], slug: str) -> dict[str, Any]:
        """The meta dict handed to `extract(raw_bytes, meta)`.

        `resolve` is a callable rather than the graph itself so that an extractor stays
        unit-testable with a plain dict stand-in and no database at all.
        """
        counter = [0]

        def resolve(alias: str, kind: str = "nse_symbol") -> str | None:
            eid = self.graph.resolve_or_queue(
                alias,
                kind=kind,
                source_slug=slug,
                as_of=cap.get("target_date"),
                context={"capture_id": cap["capture_id"]},
            )
            if eid is None:
                counter[0] += 1
            return eid

        return {
            "capture_id": cap["capture_id"],
            "source_slug": slug,
            "source_url": cap["source_url"],
            "captured_at": cap["captured_at"],
            "target_date": cap.get("target_date"),
            "content_type": cap.get("content_type"),
            "fetch_meta": cap.get("fetch_meta", {}),
            "resolve": resolve,
            "_unresolved": counter,
        }


def _validate(observations: Any, cap: dict[str, Any], version: str) -> None:
    """Catch the two mistakes an extractor makes most often, at the boundary."""
    if observations is None:
        raise TypeError("extract() returned None; it must return a list of Observations")
    for obs in observations:
        if not isinstance(obs, Observation):
            raise TypeError(f"extract() yielded {type(obs).__name__}, expected Observation")
        if obs.capture_id != cap["capture_id"]:
            raise ValueError(
                f"observation claims capture {obs.capture_id} but was extracted from "
                f"{cap['capture_id']}: provenance must be exact"
            )
        if obs.extractor_ver != version:
            raise ValueError(
                f"observation carries extractor_ver {obs.extractor_ver!r} but the module "
                f"declares {version!r}: bump one place, not two"
            )
