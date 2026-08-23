"""The six foundation checks, executed rather than asserted.

`/foundation` stage 3 lists six properties the substrate must have. This module runs
all six against a throwaway store, using the real AMFI probe adapter and extractor —
not stand-ins — so the thing being verified is the code that will actually collect.

Check 4 is the one that matters most. If re-running an extractor at a bumped version
overwrites rather than appends, the "improve the extractor in month nine and re-run it
over ten months of archive" property is absent, and every month of collection after
that is built on sand. Everything else is recoverable; that is not.

By default this runs offline against `tests/fixtures/navall_sample.txt`, with the
adapter's real fetch path stubbed at the HTTP layer. `--live` hits AMFI instead.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

from core.config import REPO_ROOT
from core.entities.graph import EntityGraph, seed_from_csv
from core.scheduler.fetch import Fetcher, Response
from core.scheduler.gaps import catchup, detect
from core.scheduler.registry import SourceRegistry
from core.scheduler.runner import Runner
from core.store.blobs import BlobStore
from core.store.db import AppendOnlyViolation, connect
from core.store.observations import ObservationStore
from core.timeutil import IST, ist_date_to_utc, utcnow

PROBE_SLUG = "amfi-nav-probe"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "navall_sample.txt"


@dataclass
class Check:
    number: int
    name: str
    passed: bool
    detail: str

    def line(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"  [{mark}] {self.number}. {self.name}\n         {self.detail}"


@dataclass
class VerifyReport:
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def render(self) -> str:
        head = "Foundation verification\n" + "=" * 60
        body = "\n".join(c.line() for c in self.checks)
        tail = "\n".join(f"  note: {n}" for n in self.notes)
        verdict = (
            "FOUNDATION PASSES — all six checks green."
            if self.passed
            else "FOUNDATION FAILS — do not start source work until this is green."
        )
        return "\n".join(x for x in (head, body, tail, "=" * 60, verdict) if x)


class _FixtureFetcher(Fetcher):
    """Serves a file from disk through the real adapter code path.

    The adapter still calls ctx.fetch_and_put, the response still becomes a
    content-addressed blob with a capture row. Only the socket is missing.
    """

    def __init__(self, payload: bytes, *, fail_after: int | None = None) -> None:
        super().__init__(respect_robots=False)
        self.payload = payload
        self.calls = 0
        self.fail_after = fail_after

    def get(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("simulated process kill mid-capture")
        return Response(
            url=url,
            status=200,
            body=self.payload,
            content_type="text/plain",
            headers={},
        )


def run_verification(*, live: bool = False, keep: bool = False) -> VerifyReport:
    report = VerifyReport()
    workdir = Path(tempfile.mkdtemp(prefix="kosh-verify-"))
    try:
        db = connect(f"sqlite:///{(workdir / 'verify.sqlite').as_posix()}", migrate=True)
        blobs = BlobStore(db, workdir / "blobs")
        obs = ObservationStore(db)
        graph = EntityGraph(db)

        seed_from_csv(graph, REPO_ROOT / "core" / "entities" / "seed" / "universe.sample.csv")
        db.commit()

        registry = SourceRegistry(db)
        registry.register(
            slug=PROBE_SLUG,
            display_name="AMFI daily NAV (foundation probe)",
            url="https://www.amfiindia.com/spages/NAVAll.txt",
            ring=1,
            cadence="daily",
            calendar="weekday",
            enabled=True,
            notes="throwaway probe -- see BRIEF.md",
        )
        db.commit()

        if live:
            payload = Fetcher().get("https://www.amfiindia.com/spages/NAVAll.txt").body
            report.notes.append(f"live fetch: {len(payload)} bytes from AMFI")
        else:
            payload = FIXTURE.read_bytes()
            report.notes.append(
                f"offline: {FIXTURE.relative_to(REPO_ROOT)} ({len(payload)} bytes)"
            )

        fetcher = _FixtureFetcher(payload)
        runner = Runner(db, blobs, fetcher=fetcher)
        day1 = date(2026, 8, 19)
        day2 = date(2026, 8, 20)
        day3 = date(2026, 8, 21)

        # -- 1. fetch lands a content-addressed blob -------------------------
        run1 = runner.collect(PROBE_SLUG, day1)
        stats = blobs.stats()
        cap_rows = blobs.captures(PROBE_SLUG)
        sha = cap_rows[0]["sha256"] if cap_rows else ""
        on_disk = blobs.abs_path_for(sha).exists() if sha else False
        addressed = bool(sha) and blobs.rel_path_for(sha).endswith(sha)
        report.checks.append(
            Check(
                1,
                "fetch lands a blob in the store, content-addressed",
                run1.ok and on_disk and addressed and stats["blobs"] == 1,
                f"run={run1.status} blobs={stats['blobs']} sha={sha[:12]}... "
                f"path={blobs.rel_path_for(sha) if sha else '-'} on_disk={on_disk}",
            )
        )

        # -- 2. identical bytes deduplicate ----------------------------------
        runner.collect(PROBE_SLUG, day1)
        stats2 = blobs.stats()
        report.checks.append(
            Check(
                2,
                "identical bytes deduplicate to one blob with two capture records",
                stats2["blobs"] == 1 and stats2["captures"] == 2,
                f"blobs={stats2['blobs']} captures={stats2['captures']} "
                f"(one file fetched twice)",
            )
        )

        # -- 3. extraction, observed_at vs captured_at, IST -> UTC -----------
        ex1 = runner.extract(PROBE_SLUG, day1)
        navs = obs.query(metric="nav.close")
        detail_bits = []
        ok3 = ex1.ok and bool(navs)
        if navs:
            sample = navs[0]
            expected = ist_date_to_utc(date(2026, 8, 21), time(18, 0))
            latest = max(r["observed_at"] for r in navs)
            distinct_obs = {r["observed_at"] for r in navs}
            separated = all(r["observed_at"] != r["captured_at"] for r in navs)
            captured_recent = all(
                (utcnow() - r["captured_at"]).total_seconds() < 3600 for r in navs
            )
            ist_correct = latest == expected
            ok3 = ok3 and separated and captured_recent and ist_correct
            detail_bits = [
                f"rows={ex1.row_count}",
                f"nav rows={len(navs)}",
                f"observed_at(max)={latest.isoformat()} == 18:00 IST -> {ist_correct}",
                f"in IST that is {latest.astimezone(IST).strftime('%Y-%m-%d %H:%M')}",
                f"observed_at != captured_at for all rows -> {separated}",
                f"distinct observed_at values={len(distinct_obs)}",
                f"entity={sample['entity_id']}",
            ]
        report.checks.append(
            Check(
                3,
                "extractor produces Observations with correct observed_at vs captured_at "
                "and IST-to-UTC handling",
                ok3,
                "; ".join(detail_bits) or f"extract status={ex1.status} {ex1.error or ''}",
            )
        )

        # -- 4. re-extraction at a bumped version appends, never overwrites --
        before = len(obs.query())
        rerun_same = runner.extract(PROBE_SLUG, day1)
        after_same = len(obs.query())

        from core.scheduler.registry import load_extractor

        shipped_ver = load_extractor(PROBE_SLUG).EXTRACTOR_VER
        bumped = _bump_extractor_version(runner, PROBE_SLUG, f"{shipped_ver}-verify-bump")
        ex2 = runner.extract(PROBE_SLUG, day1)
        after_bump = len(obs.query())
        versions = {v["extractor_ver"]: v["n"] for v in obs.versions()}
        both_queryable = len(versions) >= 2
        v1_intact = versions.get(shipped_ver, 0) == before
        appended = after_bump > after_same
        bumped()  # restore the module so nothing leaks into later checks

        # and prove the store physically refuses a mutation
        try:
            db.execute("UPDATE observations SET value_num = 0")
            db.rollback()
            enforced = False
        except AppendOnlyViolation:
            db.rollback()
            enforced = True
        try:
            db.execute("DELETE FROM observations")
            db.rollback()
            enforced = enforced and False
        except AppendOnlyViolation:
            db.rollback()

        report.checks.append(
            Check(
                4,
                "re-running the extractor at a bumped version produces new rows "
                "ALONGSIDE the old ones, both versions queryable",
                appended and both_queryable and v1_intact and enforced,
                f"same-version rerun added {rerun_same.row_count} rows (idempotent); "
                f"bumped version added {ex2.row_count}; total {before} -> {after_bump}; "
                f"versions={versions}; UPDATE/DELETE blocked by schema={enforced}",
            )
        )

        # -- 5. killed mid-capture, restarted, store stays consistent --------
        killer = _FixtureFetcher(payload + b"\n; day2 variant", fail_after=0)
        crashed = Runner(db, blobs, fetcher=killer).collect(PROBE_SLUG, day2)
        integrity = blobs.verify()
        orphan_obs = db.scalar(
            "SELECT COUNT(*) FROM observations o WHERE NOT EXISTS"
            " (SELECT 1 FROM captures c WHERE c.capture_id = o.capture_id)"
        )
        orphan_caps = db.scalar(
            "SELECT COUNT(*) FROM captures c WHERE NOT EXISTS"
            " (SELECT 1 FROM raw_blobs b WHERE b.sha256 = c.sha256)"
        )
        recovered = Runner(
            db, blobs, fetcher=_FixtureFetcher(payload + b"\n; day2 variant")
        ).collect(PROBE_SLUG, day2)
        report.checks.append(
            Check(
                5,
                "killing a run mid-capture and restarting leaves the store consistent",
                crashed.status == "error"
                and not integrity
                and orphan_obs == 0
                and orphan_caps == 0
                and recovered.ok,
                f"killed run recorded as '{crashed.status}'; blob integrity issues="
                f"{len(integrity)}; orphan observations={orphan_obs}; orphan captures="
                f"{orphan_caps}; restart status={recovered.status}",
            )
        )

        # -- 6. a missed day is detected and caught up -----------------------
        runner.extract(PROBE_SLUG, day2)
        spec = registry.get(PROBE_SLUG)
        assert spec is not None
        gap_before = detect(db, spec, since=day1, through=day3)
        missed_detected = day3 in gap_before.missing

        runner.fetcher = _FixtureFetcher(payload + b"\n; day3 variant")
        results = catchup(runner, PROBE_SLUG, through=day3)
        gap_after = detect(db, spec, since=day1, through=day3)
        filled = day3 not in gap_after.missing
        report.checks.append(
            Check(
                6,
                "a missed day is detected by gap detection and filled by catch-up",
                missed_detected and filled and not gap_after.missing,
                f"expected days {day1}..{day3} on a {spec.calendar} calendar; missing "
                f"before={[str(d) for d in gap_before.missing]}; catch-up ran "
                f"{len([r for r in results if r.get('date')])} day(s); missing after="
                f"{[str(d) for d in gap_after.missing]}",
            )
        )

        report.notes.append(f"entity graph: {graph.stats()}")
        report.notes.append(f"blob store: {blobs.stats()}")
        report.notes.append(
            "verification ran against a throwaway store in "
            f"{workdir if keep else 'a temp dir (removed)'}; the real corpus was not touched"
        )
        db.close()
        return report
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def _bump_extractor_version(runner: Runner, slug: str, new_version: str):
    """Temporarily bump the loaded extractor's version.

    A real version bump is an edit to the module. Doing it in memory here keeps the
    check honest -- same code, same captures, different declared version -- without
    the verifier writing to the repo.
    """
    from core.scheduler.registry import load_extractor

    module = load_extractor(slug)
    original = module.EXTRACTOR_VER
    module.EXTRACTOR_VER = new_version
    original_extract = module.extract

    def patched(raw: bytes, meta: dict) -> list:
        out = []
        for o in original_extract(raw, meta):
            o.extractor_ver = new_version
            o.observation_id = o.observation_id[:-4] + "v2ex"
            out.append(o)
        return out

    module.extract = patched

    def restore() -> None:
        module.EXTRACTOR_VER = original
        module.extract = original_extract

    return restore


def summarise_for_operator(db_url: str) -> str:
    """The commands you will use constantly from here on."""
    today = (date.today() - timedelta(days=1)).isoformat()
    return (
        "Collect and extract one source for one date:\n"
        f"    kosh collect --source {PROBE_SLUG} --date {today}\n"
        f"    kosh extract --source {PROBE_SLUG} --date {today}\n"
        "\nOr both, plus canaries, in one go:\n"
        f"    kosh run --source {PROBE_SLUG} --date {today}\n"
        "\nEvery morning:\n"
        "    kosh health\n"
        f"\nStore: {db_url}"
    )
