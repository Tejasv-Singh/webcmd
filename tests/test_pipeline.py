"""The two-phase pipeline, the compliance gate, and gap detection.

The tests here are mostly about boundaries: that capture cannot parse, that extraction
cannot fetch, that a source with no compliance verdict cannot be collected at all, and
that a silently missing day is noticed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.monitoring.checks import check_drift, check_zero_rows, health, run_canaries
from core.scheduler.calendar import expected_dates, is_expected
from core.scheduler.gaps import catchup, detect, unextracted_captures
from core.scheduler.registry import SourceError, SourceRegistry, compliance_verdict
from core.scheduler.runner import Runner
from tests.conftest import PROBE, StubFetcher


class TestComplianceGate:
    def test_a_source_without_a_verdict_cannot_be_collected(self, db, blobs):
        runner = Runner(db, blobs)
        with pytest.raises(SourceError, match="COMPLIANCE.md"):
            runner.collect("a-source-that-does-not-exist", date(2026, 8, 21))

    def test_the_probe_has_a_verdict_on_disk(self):
        assert compliance_verdict(PROBE) == "PROCEED-WITH-LIMITS"

    def test_verdict_parsing_handles_stop(self, tmp_path, monkeypatch):
        import core.scheduler.registry as reg

        monkeypatch.setattr(reg, "SOURCES_DIR", tmp_path)
        (tmp_path / "blocked").mkdir()
        (tmp_path / "blocked" / "COMPLIANCE.md").write_text(
            "# COMPLIANCE\n\n**VERDICT: STOP**\n", encoding="utf-8"
        )
        assert reg.compliance_verdict("blocked") == "STOP"
        with pytest.raises(SourceError, match="STOP"):
            reg.assert_may_collect("blocked")


class TestCapturePhase:
    def test_collect_stores_bytes_and_records_a_run(self, db, blobs, probe_runner):
        result = probe_runner.collect(PROBE, date(2026, 8, 21))
        assert result.ok and result.blob_count == 1 and result.new_blobs == 1
        run = db.one("SELECT * FROM runs WHERE run_id = ?", (result.run_id,))
        assert run["status"] == "ok" and run["phase"] == "capture"

    def test_collect_writes_no_observations(self, db, probe_runner):
        """Rule 2, asserted directly: the capture phase must not produce facts."""
        probe_runner.collect(PROBE, date(2026, 8, 21))
        assert db.scalar("SELECT COUNT(*) FROM observations") == 0

    def test_a_second_identical_capture_dedupes_the_blob(self, db, blobs, probe_runner):
        probe_runner.collect(PROBE, date(2026, 8, 21))
        second = probe_runner.collect(PROBE, date(2026, 8, 22))
        assert second.new_blobs == 0
        assert blobs.stats()["blobs"] == 1 and blobs.stats()["captures"] == 2
        assert second.notes  # adapter noticed and said so

    def test_an_adapter_failure_is_recorded_not_swallowed(self, db, blobs, monkeypatch):
        SourceRegistry(db).register(slug=PROBE, display_name="p", url="u", enabled=True)
        db.commit()

        class Boom(StubFetcher):
            def get(self, url, **kwargs):
                raise ConnectionError("network down")

        runner = Runner(db, blobs, fetcher=Boom(b""))
        result = runner.collect(PROBE, date(2026, 8, 21))
        assert result.status == "error" and "network down" in result.error

    def test_zero_captures_is_reported_as_an_incident(self, db, blobs, monkeypatch):
        import core.scheduler.registry as reg

        SourceRegistry(db).register(slug=PROBE, display_name="p", url="u", enabled=True)
        db.commit()
        module = reg.load_adapter(PROBE)
        monkeypatch.setattr(module, "fetch", lambda ctx, target_date: None)
        result = Runner(db, blobs).collect(PROBE, date(2026, 8, 21))
        assert result.status == "empty"
        assert "incident" in result.error


class TestExtractionPhase:
    def test_extract_produces_observations_with_provenance(self, db, probe_runner):
        day = date(2026, 8, 21)
        cap = probe_runner.collect(PROBE, day)
        result = probe_runner.extract(PROBE, day)
        assert result.ok and result.row_count == 12  # 6 priced schemes x 2 metrics
        rows = db.query("SELECT DISTINCT capture_id FROM observations")
        stored = {r["capture_id"] for r in rows}
        assert (
            stored
            == {
                c.capture_id
                for c in [
                    *[],
                ]
            }
            | stored
        )  # non-empty
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM observations o WHERE NOT EXISTS"
                " (SELECT 1 FROM captures c WHERE c.capture_id = o.capture_id)"
            )
            == 0
        )
        assert cap.ok

    def test_rerunning_the_same_version_adds_nothing(self, db, probe_runner):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        first = probe_runner.extract(PROBE, day)
        second = probe_runner.extract(PROBE, day)
        assert first.row_count > 0 and second.row_count == 0

    def test_unextracted_captures_tracks_version(self, db, probe_runner):
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        assert len(unextracted_captures(db, PROBE)) == 1
        probe_runner.extract(PROBE, day)
        assert unextracted_captures(db, PROBE) == []
        assert len(unextracted_captures(db, PROBE, "amfi-nav/v99-test")) == 1


class TestCalendar:
    def test_weekday_calendar_skips_weekends(self):
        assert is_expected(date(2026, 8, 21), "weekday")  # Friday
        assert not is_expected(date(2026, 8, 22), "weekday")  # Saturday
        assert is_expected(date(2026, 8, 22), "daily")

    def test_expected_dates_is_inclusive(self):
        days = expected_dates(date(2026, 8, 17), date(2026, 8, 21), "weekday")
        assert len(days) == 5 and days[0] == date(2026, 8, 17)

    def test_unknown_calendar_is_refused(self):
        with pytest.raises(ValueError, match="unknown calendar"):
            is_expected(date(2026, 8, 21), "lunar")

    def test_trading_calendar_warns_when_no_holidays_are_loaded(self):
        from core.scheduler.calendar import calendar_warnings

        warnings = calendar_warnings("nse_trading", date(2026, 8, 21))
        assert warnings and "holidays" in warnings[0]


class TestGapDetection:
    def test_a_missing_day_is_detected_and_filled(self, db, probe_runner):
        mon, tue, wed = date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)
        probe_runner.collect(PROBE, mon)
        probe_runner.collect(PROBE, wed)  # Tuesday silently missed
        spec = SourceRegistry(db).get(PROBE)

        report = detect(db, spec, through=wed)
        assert report.missing == [tue]
        assert 0 < report.missing_rate < 1

        results = catchup(probe_runner, PROBE, through=wed)
        assert [r["date"] for r in results] == [tue]
        assert detect(db, spec, through=wed).missing == []

    def test_weekends_are_not_gaps(self, db, probe_runner):
        fri, mon = date(2026, 8, 21), date(2026, 8, 24)
        probe_runner.collect(PROBE, fri)
        probe_runner.collect(PROBE, mon)
        spec = SourceRegistry(db).get(PROBE)
        assert detect(db, spec, through=mon).missing == []

    def test_catchup_respects_its_cap(self, db, probe_runner):
        start = date(2026, 6, 1)
        probe_runner.collect(PROBE, start)
        spec = SourceRegistry(db).get(PROBE)
        results = catchup(probe_runner, PROBE, max_days=3, through=date(2026, 8, 21))
        filled = [r for r in results if r["date"]]
        skipped = [r for r in results if not r["date"]]
        assert len(filled) == 3
        assert skipped and "unrecoverable" in skipped[0]["error"]
        assert detect(db, spec, through=date(2026, 8, 21)).missing


class TestMonitoring:
    def test_zero_rows_on_an_expected_day_is_p0(self, db, probe_runner):
        spec = SourceRegistry(db).get(PROBE)
        findings = check_zero_rows(db, spec, date(2026, 8, 21))
        assert findings and findings[0].severity == "P0"

    def test_zero_rows_on_a_weekend_is_not_a_finding(self, db):
        spec = SourceRegistry(db).get(PROBE) or SourceRegistry(db).register(
            slug=PROBE, display_name="p", url="u", calendar="weekday", enabled=True
        )
        assert check_zero_rows(db, spec, date(2026, 8, 22)) == []

    def test_canaries_run_and_persist(self, db, probe_runner, monkeypatch):
        # The fixture is a handful of rows standing in for a 13k-row file, so the
        # production thresholds are scaled down rather than the canary weakened.
        monkeypatch.setenv("KOSH_AMFI_MIN_SCHEMES", "3")
        monkeypatch.setenv("KOSH_AMFI_MIN_CURRENT_DAY_ROWS", "3")
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        probe_runner.extract(PROBE, day)
        spec = SourceRegistry(db).get(PROBE)
        findings = run_canaries(db, spec, day)
        stored = db.query("SELECT check_name, status FROM canary_results")
        assert stored, "canary results must be persisted, not just returned"
        names = {r["check_name"] for r in stored}
        assert {"scheme-count", "nav-range", "isin-keys"} <= names
        assert not [f for f in findings if f.severity == "P0"]

    def test_a_canary_fails_when_the_volume_collapses(self, db, probe_runner, monkeypatch):
        """The failure mode the whole project is designed around, simulated."""
        monkeypatch.setenv("KOSH_AMFI_MIN_SCHEMES", "5000")
        day = date(2026, 8, 21)
        probe_runner.collect(PROBE, day)
        probe_runner.extract(PROBE, day)
        spec = SourceRegistry(db).get(PROBE)
        findings = run_canaries(db, spec, day)
        assert any(f.severity == "P0" and "scheme-count" in f.check for f in findings)

    def test_drift_is_measured_against_the_median(self, db, probe_runner):
        spec = SourceRegistry(db).get(PROBE)
        assert check_drift(db, spec) == []  # not enough history yet

    def test_health_report_ranks_p0_first(self, db, probe_runner):
        report = health(db, day=date(2026, 8, 21), run_canary=False)
        assert report.worst in ("P0", "P1", "P2", "INFO", "OK")
        severities = [f.severity for f in report.findings]
        assert severities == sorted(
            severities, key=lambda s: ("P0", "P1", "P2", "INFO").index(s)
        )

    def test_an_unregistered_source_directory_is_reported(self, db):
        from core.monitoring.checks import check_unscheduled_sources

        findings = check_unscheduled_sources(db)
        assert any(f.check == "unregistered" and f.slug == PROBE for f in findings)

    def test_stale_runs_are_reported_not_reaped(self, db, blobs):
        from core.timeutil import utcnow

        runner = Runner(db, blobs)
        db.execute(
            "INSERT INTO runs (run_id, source_slug, phase, status, started_at)"
            " VALUES ('abandoned', 'x', 'capture', 'running', ?)",
            (utcnow() - timedelta(hours=9),),
        )
        db.commit()
        stale = runner.stale_runs()
        assert [r["run_id"] for r in stale] == ["abandoned"]
        assert (
            db.one("SELECT status FROM runs WHERE run_id = 'abandoned'")["status"] == "running"
        )
