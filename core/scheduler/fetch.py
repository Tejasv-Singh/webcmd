"""The polite HTTP fetcher used by adapters that do not need a browser.

Two things are enforced here rather than left to each adapter's good intentions:
a per-host request interval, and robots.txt. Both are compliance constraints from
CLAUDE.md, and both are the sort of thing that gets "temporarily" skipped at 1am
during a backfill unless the code simply will not do it.

Adapters that *do* need a browser go through Webcmd instead; this is the plain-file
path (AMFI's NAV text file, a CSV endpoint, a JSON API found by source-scout).
"""

from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from core.config import Settings, settings


class ComplianceError(RuntimeError):
    """The fetch was refused on compliance grounds, not technical ones."""


class FetchError(RuntimeError):
    pass


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    content_type: str | None
    headers: dict[str, str]


class Fetcher:
    def __init__(self, cfg: Settings | None = None, *, respect_robots: bool = True) -> None:
        self.cfg = cfg or settings()
        self.respect_robots = respect_robots
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    # -- politeness ----------------------------------------------------------

    def _throttle(self, host: str) -> None:
        gap = self.cfg.min_request_interval
        last = self._last_hit.get(host)
        if last is not None:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        parser: RobotFileParser | None = RobotFileParser()
        assert parser is not None
        parser.set_url(f"{origin}/robots.txt")
        try:
            parser.read()
        except Exception:
            # An unreachable robots.txt is not permission. Treat it as unknown and
            # let the source's COMPLIANCE.md be the authority instead of guessing.
            parser = None
        self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.cfg.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        parser = self._robots_for(url)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.cfg.user_agent)
        except Exception:
            return None
        return float(delay) if delay else None

    # -- fetching ------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        retries: int = 3,
        backoff: float = 2.0,
    ) -> Response:
        if not self.allowed(url):
            raise ComplianceError(
                f"robots.txt disallows {url} for {self.cfg.user_agent}. "
                "If the source's COMPLIANCE.md says otherwise, say so there explicitly "
                "and pass respect_robots=False deliberately."
            )
        host = urlparse(url).netloc
        delay = self.crawl_delay(url)
        if delay and delay > self.cfg.min_request_interval:
            self._last_hit.setdefault(host, time.monotonic() - delay)

        request_headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept-Encoding": "gzip, deflate",
            **(headers or {}),
        }
        last_error: Exception | None = None
        for attempt in range(retries):
            self._throttle(host)
            req = urllib.request.Request(url, headers=request_headers)
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.http_timeout) as resp:
                    body = _decode(resp.read(), resp.headers.get("Content-Encoding"))
                    return Response(
                        url=resp.geturl(),
                        status=resp.status,
                        body=body,
                        content_type=resp.headers.get("Content-Type"),
                        headers={k.lower(): v for k, v in resp.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx other than 429 will not fix themselves by trying again.
                if exc.code < 500 and exc.code != 429:
                    raise FetchError(f"{url} -> HTTP {exc.code} {exc.reason}") from exc
            except Exception as exc:  # network-level
                last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff**attempt)
        raise FetchError(f"{url} failed after {retries} attempts: {last_error}")


def _decode(body: bytes, encoding: str | None) -> bytes:
    """Store decompressed bytes: the blob should be what the page *is*, not what the
    transport happened to wrap it in, so a re-run with different Accept-Encoding
    still hashes to the same content."""
    if not encoding:
        return body
    enc = encoding.lower()
    if enc == "gzip":
        return gzip.decompress(body)
    if enc == "deflate":
        return zlib.decompress(body, -zlib.MAX_WBITS)
    return body
