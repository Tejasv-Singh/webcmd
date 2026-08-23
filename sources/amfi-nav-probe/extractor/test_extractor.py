"""Extractor tests — no network, no browser, no database.

This file is the proof that CLAUDE.md's extractor contract holds: `extract` is a pure
function of (raw_bytes, meta), so it can be tested against a handful of literal bytes
and re-run over a blob captured a year ago. If a change ever makes these tests need a
fixture database or a live fetch, the change is wrong, not the tests.

Both file layouts are exercised deliberately — see TestLayoutDrift for why.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from core.scheduler.registry import load_extractor
from core.timeutil import IST, ist_date_to_utc

# Loaded the way the runner loads it, by path -- so the test exercises the same module
# object the pipeline will.
_module = load_extractor("amfi-nav-probe")
EXTRACTOR_VER = _module.EXTRACTOR_VER
extract = _module.extract

META = {
    "source_url": "https://www.amfiindia.com/spages/NAVAll.txt",
    "capture_id": "cap-under-test",
    "captured_at": None,
}

# The layout AMFI serves today, verbatim from a live capture (note the space after the
# slash -- the header is not tidy, and the parser must not assume it is).
HEADER = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
    "Scheme Name;Plan;Option;Net Asset Value;Date\n"
)

# The layout the file used historically. Blobs in the archive still have this shape.
LEGACY_HEADER = (
    "Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;"
    "Scheme Name;Net Asset Value;Date\n"
)


def rows(*lines: str) -> bytes:
    """Current 8-column layout: code;isin;isin2;name;plan;option;nav;date"""
    return (HEADER + "\n".join(lines) + "\n").encode()


def legacy_rows(*lines: str) -> bytes:
    """Historic 6-column layout: code;isin;isin2;name;nav;date"""
    return (LEGACY_HEADER + "\n".join(lines) + "\n").encode()


def navs(observations):
    return [o for o in observations if o.metric == "nav.close"]


def row(isin="INF000TEST01", nav="10.0", nav_date="21-Aug-2026", name="Fund", code="1"):
    return f"{code};{isin};-;{name};Direct Plan;Growth;{nav};{nav_date}"


class TestHappyPath:
    def test_one_row_becomes_a_nav_and_a_name_observation(self):
        out = extract(rows(row(nav="1234.5678", name="Testfund Liquid", code="100001")), META)
        assert [o.metric for o in out] == ["nav.close", "fund.scheme_name"]
        nav = out[0]
        assert nav.entity_id == "ISIN:INF000TEST01"
        assert nav.value == 1234.5678
        assert nav.capture_id == "cap-under-test"
        assert nav.extractor_ver == EXTRACTOR_VER

    def test_observed_at_is_the_row_date_in_ist_not_the_capture_time(self):
        out = navs(extract(rows(row(nav_date="20-Aug-2026")), META))
        assert out[0].observed_at == ist_date_to_utc(date(2026, 8, 20), time(18, 0))
        # and it round-trips back to an Indian evening, not a UTC one
        assert (
            out[0].observed_at.astimezone(IST).strftime("%Y-%m-%d %H:%M") == "2026-08-20 18:00"
        )

    def test_thousands_separators_are_handled(self):
        out = navs(extract(rows(row(nav="1,024.5000")), META))
        assert out[0].value == 1024.5

    def test_plan_and_option_are_carried_on_the_descriptor(self):
        descriptor = [o for o in extract(rows(row()), META) if o.metric == "fund.scheme_name"][
            0
        ]
        assert descriptor.value["plan"] == "Direct Plan"
        assert descriptor.value["option"] == "Growth"
        assert descriptor.value["amfi_code"] == "1"


class TestRowsThatMustBeSkipped:
    @pytest.mark.parametrize(
        "line,reason",
        [
            (row(nav="N.A."), "unpriced scheme"),
            (row(nav=""), "blank NAV"),
            (row(isin="-"), "no ISIN"),
            (row(isin="NOTANISIN"), "malformed ISIN"),
            (row(nav_date="not-a-date"), "unparseable date"),
            (row(nav="0"), "zero NAV"),
            (row(nav="-5.0"), "negative NAV"),
        ],
    )
    def test_bad_rows_produce_nothing_rather_than_a_fabricated_zero(self, line, reason):
        assert extract(rows(line), META) == [], reason

    def test_category_and_fund_house_headings_are_ignored(self):
        raw = rows(
            "",
            "Open Ended Schemes(Debt Scheme - Liquid Fund)",
            "Some Asset Management Limited",
            row(),
        )
        assert len(navs(extract(raw, META))) == 1

    def test_truncated_rows_are_ignored(self):
        assert extract(rows("1;INF000TEST01;-;Fund"), META) == []


class TestDeduplication:
    def test_the_same_isin_twice_in_one_file_yields_one_observation(self):
        raw = rows(row(name="Fund A"), row(name="Fund A duplicate line", code="2"))
        assert len(navs(extract(raw, META))) == 1

    def test_the_same_isin_on_different_dates_yields_two(self):
        raw = rows(row(nav_date="20-Aug-2026"), row(nav="10.5"))
        assert len(navs(extract(raw, META))) == 2


class TestEncoding:
    def test_a_utf8_bom_does_not_break_the_first_row(self):
        raw = b"\xef\xbb\xbf" + rows(row())
        assert len(navs(extract(raw, META))) == 1

    def test_undecodable_bytes_do_not_raise(self):
        """Real files occasionally carry a stray non-UTF-8 byte in a scheme name.
        Losing the whole day's capture over one bad character would be absurd."""
        raw = rows(row(name="NAME")).replace(b"NAME", b"\xff\xfe")
        assert len(navs(extract(raw, META))) == 1

    def test_an_empty_file_yields_nothing(self):
        assert extract(b"", META) == []


class TestLayoutDrift:
    """The reason this extractor parses by header name instead of by position.

    v1 read fixed indices from a hand-written brief describing the six-column layout.
    The live file has eight — AMFI added Plan and Option between the name and the NAV —
    so v1 read "Direct Plan" as the NAV and returned **zero rows from a good 1.5 MB
    capture**. Nothing was lost, because the raw bytes were archived and could simply
    be re-extracted at v2. That is the archive earning its keep, and these tests pin
    the behaviour in both directions.
    """

    def test_the_current_eight_column_layout_reads_nav_not_plan(self):
        out = navs(extract(rows(row(nav="1234.5678")), META))
        assert len(out) == 1 and out[0].value == 1234.5678

    def test_the_historic_six_column_layout_still_extracts(self):
        raw = legacy_rows("1;INF000TEST01;INF000TEST02;Testfund Liquid;1234.5678;21-Aug-2026")
        out = navs(extract(raw, META))
        assert len(out) == 1
        assert out[0].value == 1234.5678
        assert out[0].entity_id == "ISIN:INF000TEST01"

    def test_a_file_with_no_header_falls_back_to_the_historic_layout(self):
        raw = b"1;INF000TEST01;INF000TEST02;Testfund Liquid;1234.5678;21-Aug-2026\n"
        assert len(navs(extract(raw, META))) == 1

    def test_a_header_with_reordered_columns_is_followed(self):
        """Position is not trusted anywhere, including the order of the columns."""
        raw = (
            b"Date;Net Asset Value;Scheme Name;ISIN Div Reinvestment;"
            b"ISIN Div Payout/ ISIN Growth;Scheme Code\n"
            b"21-Aug-2026;77.25;Testfund;-;INF000TEST01;1\n"
        )
        out = navs(extract(raw, META))
        assert len(out) == 1 and out[0].value == 77.25


def test_the_extractor_declares_a_version():
    assert EXTRACTOR_VER.startswith("amfi-nav/v")


def test_the_extractor_imports_nothing_that_touches_the_network():
    """Rule 2, enforced as a test as well as by the agent's tool grant."""
    import pathlib

    src = pathlib.Path(__file__).with_name("extractor.py").read_text(encoding="utf-8")
    for banned in ("requests", "urllib", "httpx", "webcmd", "socket", "selenium"):
        assert banned not in src, f"extractor imports {banned}: extraction must be offline"
