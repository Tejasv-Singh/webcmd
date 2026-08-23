"""The entity graph: resolve on ISIN, never on a display name, defer when unsure."""

from __future__ import annotations

from datetime import date

import pytest

from core.config import REPO_ROOT
from core.entities.graph import (
    entity_id_for,
    is_isin,
    normalise,
    parse_nse_equity_list,
    seed_from_csv,
)

SEED = REPO_ROOT / "core" / "entities" / "seed" / "universe.sample.csv"


class TestNormalisation:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("Infosys Limited", "Infosys Ltd."),
            ("Infosys Ltd", "INFOSYS"),
            ("Tata Consultancy Services Limited", "tata consultancy services ltd"),
            ("The Ramco Cements Limited", "Ramco Cements Ltd"),
        ],
    )
    def test_company_suffixes_fold_together(self, a, b):
        assert normalise("legal_name", a) == normalise("legal_name", b)

    def test_different_companies_do_not_fold_together(self):
        assert normalise("legal_name", "Bajaj Finance Ltd") != normalise(
            "legal_name", "Bajaj Finserv Ltd"
        )

    def test_codes_are_not_folded_aggressively(self):
        """A trailing '.0' on a BSE code is a spreadsheet bug and should stay visible."""
        assert normalise("bse_code", "500209") != normalise("bse_code", "500209.0")

    def test_isin_recognition(self):
        assert is_isin("INE009A01021")
        assert is_isin("INF209KB1ZH7")  # mutual fund ISINs start INF
        assert not is_isin("INFY")
        assert not is_isin("US0378331005")

    def test_entity_ids_prefer_isin(self):
        assert entity_id_for("INE009A01021", "Infosys") == "ISIN:INE009A01021"
        assert entity_id_for(None, "Zomato") == "ORG:zomato"
        with pytest.raises(ValueError):
            entity_id_for(None, None)


class TestResolution:
    @pytest.fixture(autouse=True)
    def _seed(self, graph, db):
        seed_from_csv(graph, SEED)
        db.commit()

    def test_resolves_by_symbol_isin_and_name(self, graph):
        assert graph.resolve("TESTCO", kind="nse_symbol").entity_id == "ISIN:INE000TEST01"
        assert graph.resolve("INE000TEST01").entity_id == "ISIN:INE000TEST01"
        assert graph.resolve("Testco Industries Ltd").entity_id == "ISIN:INE000TEST01"

    def test_name_matches_carry_lower_confidence_than_codes(self, graph):
        by_code = graph.resolve("TESTCO", kind="nse_symbol")
        by_name = graph.resolve("Testco Industries Limited")
        assert by_code.confidence > by_name.confidence

    def test_unknown_alias_returns_none_rather_than_guessing(self, graph):
        assert graph.resolve("SOMETHINGELSE", kind="nse_symbol") is None
        assert graph.resolve("Testco Industrial Limited") is None  # one letter off

    def test_unresolved_aliases_go_to_the_review_queue(self, graph, db):
        assert graph.resolve_or_queue("MYSTERY", kind="nse_symbol", source_slug="s") is None
        graph.resolve_or_queue("MYSTERY", kind="nse_symbol", source_slug="s")
        db.commit()
        queue = graph.queue()
        assert len(queue) == 1
        assert queue[0]["occurrences"] == 2

    def test_working_the_queue_creates_a_real_alias(self, graph, db):
        graph.resolve_or_queue("MYSTERY", kind="nse_symbol", source_slug="s")
        db.commit()
        graph.resolve_queue_item(graph.queue()[0]["queue_id"], "ISIN:INE000TEST01")
        db.commit()
        assert graph.resolve("MYSTERY", kind="nse_symbol").entity_id == "ISIN:INE000TEST01"
        assert graph.queue() == []

    def test_an_alias_cannot_be_silently_stolen(self, graph):
        with pytest.raises(ValueError, match="alias collision"):
            graph.add_alias("ISIN:INE000TEST02", "nse_symbol", "TESTCO")


class TestIdentityEvents:
    @pytest.fixture(autouse=True)
    def _seed(self, graph, db):
        seed_from_csv(graph, SEED)
        db.commit()

    def test_symbol_change_keeps_history_resolvable(self, graph, db):
        graph.apply_symbol_change(
            entity_id="ISIN:INE000TEST03",
            old_symbol="OLDNAME",
            new_symbol="NEWNAME",
            effective_date=date(2026, 3, 1),
        )
        db.commit()
        # A document filed in 2025 still resolves.
        assert graph.resolve(
            "OLDNAME", kind="nse_symbol", as_of=date(2025, 6, 1)
        ).entity_id == ("ISIN:INE000TEST03")
        # The old symbol stops resolving after the change.
        assert graph.resolve("OLDNAME", kind="nse_symbol", as_of=date(2026, 6, 1)) is None
        assert graph.resolve(
            "NEWNAME", kind="nse_symbol", as_of=date(2026, 6, 1)
        ).entity_id == ("ISIN:INE000TEST03")

    def test_delisting_closes_codes_and_marks_the_entity(self, graph, db):
        graph.apply_delisting(entity_id="ISIN:INE000TEST01", effective_date=date(2026, 5, 1))
        db.commit()
        assert graph.resolve("TESTCO", kind="nse_symbol", as_of=date(2026, 6, 1)) is None
        assert graph.resolve("TESTCO", kind="nse_symbol", as_of=date(2026, 1, 1)) is not None
        row = db.one("SELECT status FROM entities WHERE entity_id = 'ISIN:INE000TEST01'")
        assert row["status"] == "delisted"

    def test_identity_events_are_append_only(self, graph, db):
        from core.store.db import AppendOnlyViolation

        graph.record_identity_event(
            event_type="name_change",
            entity_id="ISIN:INE000TEST01",
            effective_date=date(2026, 1, 1),
        )
        db.commit()
        with pytest.raises(AppendOnlyViolation):
            db.execute("UPDATE identity_events SET event_type = 'merger'")
        db.rollback()

    def test_unknown_event_types_are_refused(self, graph):
        with pytest.raises(ValueError, match="unknown identity event"):
            graph.record_identity_event(
                event_type="rebrand",
                entity_id="ISIN:INE000TEST01",
                effective_date=date(2026, 1, 1),
            )


class TestNseEquityList:
    def test_parses_the_published_column_layout(self):
        raw = (
            b"SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,"
            b" MARKET LOT, ISIN NUMBER, FACE VALUE\n"
            b"TESTCO, Testco Industries Limited, EQ, 01-JAN-1995, 5, 1,"
            b" INE000TEST01, 5\n"
        )
        rows = parse_nse_equity_list(raw)
        assert rows == [
            {
                "isin": "INE000TEST01",
                "name": "Testco Industries Limited",
                "nse_symbol": "TESTCO",
                "series": "EQ",
                "listing_date": "01-JAN-1995",
            }
        ]

    def test_rows_without_a_symbol_are_skipped(self):
        raw = b"SYMBOL,NAME OF COMPANY,ISIN NUMBER\n,,\n"
        assert parse_nse_equity_list(raw) == []


def test_the_sample_seed_is_obviously_fake():
    """Guards against someone quietly replacing the fixture with real ISINs and
    tests then asserting against unverified data."""
    text = SEED.read_text(encoding="utf-8")
    isins = [line.split(",")[0] for line in text.splitlines()[1:] if line.split(",")[0]]
    assert isins and all(i.startswith("INE000TEST") for i in isins)
