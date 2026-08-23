"""Time handling.

Market data is IST-native; the store is UTC. Every conversion goes through here so
the rule is enforced in one place rather than remembered in twenty.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = UTC


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """Naive datetimes are rejected, not guessed at."""
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime {dt!r}: say what timezone it is in")
    return dt.astimezone(UTC)


def ist(dt: datetime) -> datetime:
    """For display only. Never store the result."""
    return to_utc(dt).astimezone(IST)


def ist_naive_to_utc(dt: datetime) -> datetime:
    """A wall-clock time read off an Indian website -> UTC."""
    if dt.tzinfo is not None:
        raise ValueError("already tz-aware; use to_utc()")
    return dt.replace(tzinfo=IST).astimezone(UTC)


def ist_date_to_utc(d: date, at: time | None = None) -> datetime:
    """An IST calendar date (optionally a wall-clock time on it) -> UTC instant."""
    return datetime.combine(d, at or time(0, 0), tzinfo=IST).astimezone(UTC)


def ist_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[start, end) of an IST calendar day, in UTC. For 'what happened on 2026-08-21'."""
    return ist_date_to_utc(d), ist_date_to_utc(d + timedelta(days=1))


def ist_today() -> date:
    return datetime.now(IST).date()


def iso(dt: datetime) -> str:
    """Canonical storage form: UTC, ISO-8601, microsecond precision."""
    return to_utc(dt).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Read a timestamp back, whichever dialect produced it."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fmt_ist(value: str | datetime | None) -> str:
    """Display helper: UTC in, IST out, labelled."""
    dt = parse_ts(value)
    return "-" if dt is None else ist(dt).strftime("%Y-%m-%d %H:%M IST")
