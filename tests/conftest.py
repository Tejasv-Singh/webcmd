"""Shared fixtures.

Every test runs against a throwaway SQLite store and a throwaway blob directory. No
test may touch the real corpus, and none of them need Postgres, a browser or a network
connection -- if the suite ever grows a dependency on any of those, the thing that
made it worth having is gone.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from core.config import REPO_ROOT
from core.entities.graph import EntityGraph
from core.scheduler.fetch import Fetcher, Response
from core.scheduler.registry import SourceRegistry
from core.scheduler.runner import Runner
from core.store.blobs import BlobStore
from core.store.db import connect
from core.store.observations import Observation, ObservationStore
from core.timeutil import ist_date_to_utc

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "navall_sample.txt"
PROBE = "amfi-nav-probe"


@pytest.fixture
def db(tmp_path: Path):
    conn = connect(f"sqlite:///{(tmp_path / 'test.sqlite').as_posix()}", migrate=True)
    yield conn
    conn.close()


@pytest.fixture
def blobs(db, tmp_path: Path) -> BlobStore:
    return BlobStore(db, tmp_path / "blobs")


@pytest.fixture
def obs(db) -> ObservationStore:
    return ObservationStore(db)


@pytest.fixture
def graph(db) -> EntityGraph:
    return EntityGraph(db)


@pytest.fixture
def navall() -> bytes:
    return FIXTURE.read_bytes()


class StubFetcher(Fetcher):
    """Serves fixed bytes, so adapters can be exercised without a network."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(respect_robots=False)
        self.payload = payload
        self.calls = 0

    def get(self, url: str, **kwargs) -> Response:  # type: ignore[override]
        self.calls += 1
        return Response(
            url=url, status=200, body=self.payload, content_type="text/plain", headers={}
        )


@pytest.fixture
def probe_runner(db, blobs, navall) -> Runner:
    SourceRegistry(db).register(
        slug=PROBE,
        display_name="AMFI NAV probe",
        url="https://www.amfiindia.com/spages/NAVAll.txt",
        cadence="daily",
        calendar="weekday",
        enabled=True,
    )
    db.commit()
    return Runner(db, blobs, fetcher=StubFetcher(navall))


@pytest.fixture
def capture(blobs):
    """One capture to hang observations off."""
    return blobs.put(
        b"raw bytes",
        source_slug="test-source",
        source_url="https://example.test/page",
        target_date=date(2026, 8, 21),
    )


def make_obs(capture_id: str, **overrides) -> Observation:
    kwargs = {
        "entity_id": "ISIN:INE000TEST01",
        "metric": "nav.close",
        "value": 100.0,
        "observed_at": ist_date_to_utc(date(2026, 8, 21)),
        "source_url": "https://example.test/page",
        "capture_id": capture_id,
        "extractor_ver": "test/v1",
    }
    kwargs.update(overrides)
    return Observation(**kwargs)


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
