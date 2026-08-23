"""Database access with one thin dialect layer over SQLite and Postgres.

Why both: tests and `kosh verify-foundation` must run on a clean machine with no
services (SQLite), while the real corpus lives in Postgres. The layer is deliberately
tiny -- SQL is written once with `?` placeholders and translated, timestamps are
adapted on write, and that is the whole abstraction. No ORM.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core.config import REPO_ROOT, settings
from core.timeutil import iso, utcnow

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class AppendOnlyViolation(RuntimeError):
    """Something tried to mutate history. The schema stopped it.

    Raised uniformly for both dialects so callers -- and tests -- do not have to know
    whether they are talking to SQLite's RAISE(ABORT) or Postgres' RAISE EXCEPTION.
    """


class Database:
    """A connection plus the few dialect differences that actually matter."""

    def __init__(self, conn: Any, dialect: str, url: str) -> None:
        self.conn = conn
        self.dialect = dialect
        self.url = url

    # -- dialect adaptation --------------------------------------------------

    def _sql(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("?", "%s")
        return sql

    def _params(self, params: Sequence[Any] | None) -> tuple[Any, ...]:
        if not params:
            return ()
        out = []
        for p in params:
            if isinstance(p, datetime):
                out.append(iso(p) if self.dialect == "sqlite" else p)
            elif isinstance(p, date):
                out.append(p.isoformat())
            elif isinstance(p, bool):
                out.append(int(p) if self.dialect == "sqlite" else p)
            else:
                out.append(p)
        return tuple(out)

    # -- queries -------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        cur = self.conn.cursor()
        try:
            cur.execute(self._sql(sql), self._params(params))
        except Exception as exc:
            raise _translate(exc) from exc
        return cur

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Any:
        cur = self.conn.cursor()
        try:
            cur.executemany(self._sql(sql), [self._params(r) for r in rows])
        except Exception as exc:
            raise _translate(exc) from exc
        return cur

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.execute(sql, params).fetchone()
        return None if row is None else row[0]

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        try:
            yield self
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    # -- migrations ----------------------------------------------------------

    def migrate(self, verbose: bool = False) -> list[str]:
        """Apply pending migrations. Additive only -- see CLAUDE.md rule 1."""
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.commit()
        done = {r["name"] for r in self.query("SELECT name FROM schema_migrations")}
        applied: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob(f"*.{self.dialect}.sql")):
            name = path.name.replace(f".{self.dialect}.sql", "")
            if name in done:
                continue
            sql = path.read_text(encoding="utf-8")
            for stmt in _split_statements(sql):
                self.execute(stmt)
            self.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, iso(utcnow())),
            )
            self.commit()
            applied.append(name)
            if verbose:
                print(f"  applied {name}")
        return applied


def _translate(exc: Exception) -> Exception:
    """Give the append-only trigger one exception type across both dialects."""
    if "append-only" in str(exc):
        return AppendOnlyViolation(str(exc))
    return exc


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into statements.

    Naive semicolon splitting breaks SQLite trigger bodies (`BEGIN ... END;`) and
    Postgres function bodies (`$$ ... $$`), which is exactly where the append-only
    enforcement lives -- so both are handled explicitly.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False

    def flush() -> None:
        chunk = "\n".join(buf).strip()
        if chunk.strip(" ;\n\t"):
            statements.append(chunk)
        buf.clear()

    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if in_dollar:
            continue
        joined = " ".join(b.strip() for b in buf)
        if re.search(r"\bCREATE\s+TRIGGER\b", joined, re.I):
            if re.search(r"\bEND\s*;\s*$", joined, re.I):
                flush()
            continue
        if stripped.endswith(";"):
            flush()
    flush()
    return statements


def connect(url: str | None = None, *, migrate: bool = False) -> Database:
    url = url or settings().database_url
    if url.startswith("sqlite"):
        path_part = url.split("///", 1)[1] if "///" in url else url.split("//", 1)[-1]
        if path_part == ":memory:":
            conn = sqlite3.connect(":memory:")
        else:
            p = Path(path_part)
            if not p.is_absolute():
                p = REPO_ROOT / p
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(p), isolation_level="DEFERRED")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        db = Database(conn, "sqlite", url)
    elif url.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "Postgres URL given but psycopg is not installed: pip install 'kosh[postgres]'"
            ) from exc
        db = Database(psycopg.connect(url), "postgres", url)
    else:
        raise ValueError(f"unsupported KOSH_DATABASE_URL: {url!r}")
    if migrate:
        db.migrate()
    return db
