"""Collection calendars.

Gap detection is only as good as its idea of which days *should* have data. Getting
this wrong in either direction is expensive: too strict and every Diwali raises a P0
that trains everyone to ignore alerts; too loose and a three-week outage looks like a
quiet patch. So the calendar is explicit, and an unknown year is loud rather than
assumed.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

HOLIDAY_FILE = Path(__file__).parent / "holidays" / "nse.csv"

CALENDARS = ("daily", "weekday", "nse_trading")


@lru_cache(maxsize=1)
def _holidays() -> dict[int, set[date]]:
    by_year: dict[int, set[date]] = {}
    if not HOLIDAY_FILE.exists():
        return by_year
    with open(HOLIDAY_FILE, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("date") or "").strip()
            if not raw or raw.startswith("#"):
                continue
            d = date.fromisoformat(raw)
            by_year.setdefault(d.year, set()).add(d)
    return by_year


def reload_holidays() -> None:
    _holidays.cache_clear()


def known_holiday_years() -> set[int]:
    return set(_holidays())


def is_holiday(d: date) -> bool:
    return d in _holidays().get(d.year, set())


def is_expected(d: date, calendar: str = "weekday") -> bool:
    """Should this source have produced data on this date?"""
    if calendar == "daily":
        return True
    if calendar == "weekday":
        return d.weekday() < 5
    if calendar == "nse_trading":
        return d.weekday() < 5 and not is_holiday(d)
    raise ValueError(f"unknown calendar {calendar!r}; expected one of {CALENDARS}")


def expected_dates(start: date, end: date, calendar: str = "weekday") -> list[date]:
    """Inclusive on both ends."""
    if end < start:
        return []
    out, d = [], start
    while d <= end:
        if is_expected(d, calendar):
            out.append(d)
        d += timedelta(days=1)
    return out


def calendar_warnings(calendar: str, through: date) -> list[str]:
    """Surfaced by `kosh health`. A trading calendar with no holidays loaded for the
    current year will treat every exchange holiday as a missing day."""
    if calendar != "nse_trading":
        return []
    known = known_holiday_years()
    if through.year not in known:
        return [
            f"no NSE holidays loaded for {through.year}: every exchange holiday will "
            f"be reported as a gap. Add them to {HOLIDAY_FILE.name} from the exchange "
            "circular."
        ]
    return []
