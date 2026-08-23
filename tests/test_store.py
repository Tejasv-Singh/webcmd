"""The append-only guarantee and the blob store.

These are the tests that protect rule 1 and rule 2 of CLAUDE.md. If any of them ever
go red, stop and fix that before doing anything else -- everything downstream assumes
the properties asserted here.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.store.db import AppendOnlyViolation
from core.store.observations import Observation
from core.timeutil import ist_date_to_utc, utcnow
from tests.conftest import make_obs


class TestAppendOnly:
    def test_update_is_refused_by_the_schema(self, db, obs, capture):
        obs.append(make_obs(capture.capture_id))
        db.commit()
        with pytest.raises(AppendOnlyViolation):
            db.execute("UPDATE observations SET value_num = 999")
        db.rollback()
        assert obs.query()[0]["value"] == 100.0

    def test_delete_is_refused_by_the_schema(self, db, obs, capture):
        obs.append(make_obs(capture.capture_id))
        db.commit()
        with pytest.raises(AppendOnlyViolation):
            db.execute("DELETE FROM observations")
        db.rollback()
        assert len(obs.query()) == 1

    def test_captures_and_blobs_are_protected_too(self, db, capture):
        db.commit()
        with pytest.raises(AppendOnlyViolation):
            db.execute("DELETE FROM captures")
        db.rollback()
        with pytest.raises(AppendOnlyViolation):
            db.execute("DELETE FROM raw_blobs")
        db.rollback()

    def test_a_correction_is_a_new_row_not_an_edit(self, db, blobs, obs):
        """The scenario the whole design exists for: the source published a wrong
        number, then fixed it. Both facts stay, and 'what did we know when' works."""
        first = blobs.put(b"v1", source_slug="s", source_url="u", target_date=date(2026, 8, 21))
        obs.append(make_obs(first.capture_id, value=100.0, captured_at=utcnow()))
        later = utcnow() + timedelta(hours=2)
        second = blobs.put(
            b"v2",
            source_slug="s",
            source_url="u",
            target_date=date(2026, 8, 21),
            captured_at=later,
        )
        obs.append(make_obs(second.capture_id, value=101.5, captured_at=later))
        db.commit()

        history = obs.history("ISIN:INE000TEST01", "nav.close")
        assert [h["value"] for h in history] == [100.0, 101.5]

        # What we believed before the correction arrived.
        before = obs.current(
            "ISIN:INE000TEST01", "nav.close", known_at=later - timedelta(minutes=1)
        )
        assert before["value"] == 100.0
        # And after.
        assert obs.current("ISIN:INE000TEST01", "nav.close")["value"] == 101.5


class TestObservationValidation:
    def test_an_observation_without_provenance_is_refused(self):
        with pytest.raises(ValueError, match="capture_id"):
            make_obs("")

    def test_naive_timestamps_are_refused(self):
        from datetime import datetime

        with pytest.raises(ValueError, match="timezone-aware"):
            make_obs("cap", observed_at=datetime(2026, 8, 21, 18, 0))

    def test_extractor_version_is_required(self):
        with pytest.raises(ValueError, match="extractor_ver"):
            make_obs("cap", extractor_ver="")

    def test_numeric_fast_path_ignores_bools_and_strings(self, capture):
        assert make_obs(capture.capture_id, value=12.5).value_num == 12.5
        assert make_obs(capture.capture_id, value=True).value_num is None
        assert make_obs(capture.capture_id, value={"a": 1}).value_num is None


class TestVersioning:
    def test_same_version_reruns_are_idempotent(self, db, obs, capture):
        rows = [make_obs(capture.capture_id)]
        assert obs.append_many(rows) == 1
        assert obs.append_many([make_obs(capture.capture_id)]) == 0
        db.commit()
        assert len(obs.query()) == 1

    def test_bumped_version_lands_alongside_the_original(self, db, obs, capture):
        obs.append(make_obs(capture.capture_id, extractor_ver="test/v1", value=100.0))
        obs.append(make_obs(capture.capture_id, extractor_ver="test/v2", value=100.25))
        db.commit()
        assert len(obs.query()) == 2
        assert {v["extractor_ver"] for v in obs.versions()} == {"test/v1", "test/v2"}
        assert len(obs.query(extractor_ver="test/v1")) == 1
        latest = obs.query(latest_version_only=True)
        assert len(latest) == 1 and latest[0]["value"] == 100.25

    def test_version_ordering_is_numeric_not_lexical(self, db, obs, capture):
        obs.append(make_obs(capture.capture_id, extractor_ver="v2", value=1))
        obs.append(make_obs(capture.capture_id, extractor_ver="v10", value=2))
        db.commit()
        assert obs.query(latest_version_only=True)[0]["value"] == 2


class TestBlobStore:
    def test_identical_bytes_dedupe_to_one_blob(self, db, blobs):
        a = blobs.put(b"same", source_slug="s", source_url="u")
        b = blobs.put(b"same", source_slug="s", source_url="u")
        db.commit()
        assert a.sha256 == b.sha256
        assert a.capture_id != b.capture_id
        assert b.deduplicated and not a.deduplicated
        assert blobs.stats() == {"blobs": 1, "captures": 2, "bytes": 4, "dedup_ratio": 2.0}

    def test_content_addressing_and_fanout(self, db, blobs):
        cap = blobs.put(b"hello", source_slug="s", source_url="u")
        rel = blobs.rel_path_for(cap.sha256)
        assert rel.startswith(f"{cap.sha256[:2]}/{cap.sha256[2:4]}/")
        assert blobs.abs_path_for(cap.sha256).read_bytes() == b"hello"
        assert blobs.read_capture(cap.capture_id) == b"hello"

    def test_adapters_cannot_hand_us_parsed_objects(self, blobs):
        with pytest.raises(TypeError, match="adapters do not parse"):
            blobs.put({"already": "parsed"}, source_slug="s", source_url="u")  # type: ignore[arg-type]

    def test_integrity_check_notices_corruption(self, db, blobs):
        cap = blobs.put(b"original", source_slug="s", source_url="u")
        db.commit()
        assert blobs.verify() == []
        blobs.abs_path_for(cap.sha256).write_bytes(b"tampered")
        assert "does not match hash" in blobs.verify()[0]

    def test_missing_blob_is_loud_not_silent(self, db, blobs):
        cap = blobs.put(b"gone", source_slug="s", source_url="u")
        db.commit()
        blobs.abs_path_for(cap.sha256).unlink()
        with pytest.raises(FileNotFoundError, match="data loss"):
            blobs.read(cap.sha256)


class TestPointInTime:
    def test_known_at_excludes_rows_captured_later(self, db, blobs, obs):
        t0 = utcnow()
        early = blobs.put(b"a", source_slug="s", source_url="u", captured_at=t0)
        late = blobs.put(
            b"b", source_slug="s", source_url="u", captured_at=t0 + timedelta(days=1)
        )
        obs.append(make_obs(early.capture_id, value=1, captured_at=t0))
        obs.append(
            make_obs(
                late.capture_id,
                value=2,
                captured_at=t0 + timedelta(days=1),
                observed_at=ist_date_to_utc(date(2026, 8, 22)),
            )
        )
        db.commit()
        assert len(obs.query(known_at=t0)) == 1
        assert len(obs.query(known_at=t0 + timedelta(days=2))) == 2

    def test_extract_boundary_rejects_mismatched_provenance(self, db, capture):
        from core.scheduler.runner import _validate

        cap = {"capture_id": "the-real-one"}
        with pytest.raises(ValueError, match="provenance must be exact"):
            _validate([make_obs("a-different-one")], cap, "test/v1")

    def test_extract_boundary_rejects_version_drift(self):
        from core.scheduler.runner import _validate

        cap = {"capture_id": "cap"}
        with pytest.raises(ValueError, match="bump one place"):
            _validate([make_obs("cap", extractor_ver="test/v1")], cap, "test/v2")

    def test_extract_boundary_rejects_non_observations(self):
        from core.scheduler.runner import _validate

        with pytest.raises(TypeError):
            _validate([{"entity_id": "x"}], {"capture_id": "cap"}, "test/v1")
        with pytest.raises(TypeError, match="must return a list"):
            _validate(None, {"capture_id": "cap"}, "test/v1")


def test_observation_is_a_plain_dataclass():
    """Extractors construct these by hand; keep the constructor obvious."""
    assert Observation.__dataclass_fields__["entity_id"].type == "str"
