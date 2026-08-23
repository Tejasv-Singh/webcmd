"""Extraction backfill: improve the extractor, re-run it over the archive.

If these tests pass, the central claim of the architecture holds — that a parser
written in month nine makes month one better, without losing what month one already
said.
"""

from __future__ import annotations

from datetime import date, timedelta

from core.extract.backfill import (
    diff_against_stored,
    extract_dry,
    run_backfill,
    sample_captures,
)
from core.scheduler.registry import load_extractor
from core.store.observations import ObservationStore
from tests.conftest import PROBE, StubFetcher, weekdays

# The probe's shipped version, read rather than hardcoded: these tests are about the
# mechanism, and they should not need editing every time the extractor is improved.
CURRENT_VER = load_extractor(PROBE).EXTRACTOR_VER
BUMPED_VER = "amfi-nav/v99-test"


class TestSampling:
    def test_sampling_spreads_across_months_not_clusters(self, db, probe_runner, navall):
        base = navall
        for i, day in enumerate(weekdays(date(2026, 5, 1), 40)):
            probe_runner.fetcher = StubFetcher(base + f"\n; unique {i}".encode())
            probe_runner.collect(PROBE, day)
        ids = sample_captures(db, PROBE, 9)
        months = set()
        for cid in ids:
            cap = db.one("SELECT target_date FROM captures WHERE capture_id = ?", (cid,))
            months.add(str(cap["target_date"])[:7])
        assert len(ids) == 9
        assert len(months) >= 2, "a clustered sample hides layout changes in other months"

    def test_sampling_returns_everything_when_the_archive_is_small(self, db, probe_runner):
        probe_runner.collect(PROBE, date(2026, 8, 21))
        assert len(sample_captures(db, PROBE, 20)) == 1

    def test_sampling_is_deterministic(self, db, probe_runner, navall):
        for i, day in enumerate(weekdays(date(2026, 5, 1), 30)):
            probe_runner.fetcher = StubFetcher(navall + f"\n; {i}".encode())
            probe_runner.collect(PROBE, day)
        assert sample_captures(db, PROBE, 7) == sample_captures(db, PROBE, 7)


class TestDryRun:
    def test_dry_run_writes_nothing(self, db, blobs, probe_runner):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        ids = sample_captures(db, PROBE, 5)
        produced = extract_dry(db, PROBE, ids, blobs=blobs)
        assert produced, "dry run should still produce observations in memory"
        assert db.scalar("SELECT COUNT(*) FROM observations") == 0


class TestDiff:
    def test_identical_versions_diff_to_nothing_and_say_so(self, db, blobs, probe_runner):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        probe_runner.extract(PROBE, day)
        ids = sample_captures(db, PROBE, 5)
        diff = diff_against_stored(db, PROBE, ids, blobs=blobs)
        assert diff.added == [] and diff.removed == [] and diff.changed == []
        assert diff.unchanged > 0
        assert any("byte-identical" in w for w in diff.is_suspicious())

    def test_a_changed_value_shows_up_as_changed(self, db, blobs, probe_runner, monkeypatch):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        probe_runner.extract(PROBE, day)
        ids = sample_captures(db, PROBE, 5)

        from core.scheduler.registry import load_extractor

        module = load_extractor(PROBE)
        original = module.extract

        def rounded(raw, meta):
            out = original(raw, meta)
            for o in out:
                if o.metric == "nav.close":
                    o.value = round(o.value, 2)
            return out

        monkeypatch.setattr(module, "extract", rounded)
        monkeypatch.setattr(module, "EXTRACTOR_VER", BUMPED_VER)
        diff = diff_against_stored(db, PROBE, ids, blobs=blobs)
        assert diff.changed and not diff.removed
        assert diff.old_ver == CURRENT_VER and diff.new_ver == BUMPED_VER

    def test_a_backfill_that_loses_rows_is_flagged_as_suspicious(
        self, db, blobs, probe_runner, monkeypatch
    ):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        probe_runner.extract(PROBE, day)
        ids = sample_captures(db, PROBE, 5)

        from core.scheduler.registry import load_extractor

        module = load_extractor(PROBE)
        monkeypatch.setattr(module, "extract", lambda raw, meta: [])
        monkeypatch.setattr(module, "EXTRACTOR_VER", "amfi-nav/v99-broken")
        diff = diff_against_stored(db, PROBE, ids, blobs=blobs)
        warnings = diff.is_suspicious()
        assert warnings and "rarely finds less" in warnings[0]


class TestCommit:
    def test_backfill_appends_alongside_and_keeps_both_versions(
        self, db, blobs, probe_runner, monkeypatch
    ):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        probe_runner.extract(PROBE, day)
        obs = ObservationStore(db)
        before = len(obs.query())
        v1_rows = len(obs.query(extractor_ver=CURRENT_VER))

        from core.scheduler.registry import load_extractor

        module = load_extractor(PROBE)
        original = module.extract

        def bumped(raw, meta):
            out = original(raw, meta)
            for o in out:
                o.extractor_ver = BUMPED_VER
            return out

        monkeypatch.setattr(module, "extract", bumped)
        monkeypatch.setattr(module, "EXTRACTOR_VER", BUMPED_VER)

        result = run_backfill(db, PROBE, blobs=blobs)
        assert result["status"] == "ok" and result["rows_added"] == before
        assert len(obs.query()) == before * 2
        # the old version is untouched and still queryable
        assert len(obs.query(extractor_ver=CURRENT_VER)) == v1_rows
        assert {v["extractor_ver"] for v in obs.versions()} == {CURRENT_VER, BUMPED_VER}

    def test_backfill_is_a_no_op_when_everything_is_current(self, db, blobs, probe_runner):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        probe_runner.extract(PROBE, day)
        assert run_backfill(db, PROBE, blobs=blobs)["status"] == "nothing-to-do"

    def test_backfill_can_be_limited_to_a_window(self, db, blobs, probe_runner, navall):
        days = weekdays(date(2026, 8, 3), 6)
        for i, day in enumerate(days):
            probe_runner.fetcher = StubFetcher(navall + f"\n; {i}".encode())
            probe_runner.collect(PROBE, day)
        result = run_backfill(db, PROBE, since=days[0], until=days[2], blobs=blobs)
        assert result["captures_processed"] == 3


def test_a_correction_and_a_reextraction_are_distinguishable(db, probe_runner, navall):
    """Two different things that both add rows: the source changed its mind
    (new capture), versus we changed our parser (new extractor_ver). The store has to
    tell them apart or provenance is meaningless."""
    day = date(2026, 8, 21)
    probe_runner.collect(PROBE, day)
    probe_runner.extract(PROBE, day)
    probe_runner.fetcher = StubFetcher(navall.replace(b"1234.5678", b"1240.0000"))
    probe_runner.collect(PROBE, day + timedelta(days=0))
    probe_runner.extract(PROBE, day)

    obs = ObservationStore(db)
    history = obs.history("ISIN:INF000TEST01", "nav.close")
    assert len(history) == 2
    assert {h["extractor_ver"] for h in history} == {CURRENT_VER}
    assert len({h["capture_id"] for h in history}) == 2
    assert obs.current("ISIN:INF000TEST01", "nav.close")["value"] == 1240.0
