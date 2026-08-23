"""Scheduling: what should run, what did run, what silently did not."""

from core.scheduler.calendar import expected_dates, is_expected
from core.scheduler.fetch import ComplianceError, Fetcher, FetchError
from core.scheduler.gaps import GapReport, catchup, detect, detect_all, freshness
from core.scheduler.registry import (
    SourceError,
    SourceRegistry,
    SourceSpec,
    assert_may_collect,
    compliance_verdict,
    load_adapter,
    load_canary,
    load_extractor,
    parse_date_arg,
)
from core.scheduler.runner import CaptureContext, Runner, RunResult

__all__ = [
    "CaptureContext",
    "ComplianceError",
    "FetchError",
    "Fetcher",
    "GapReport",
    "RunResult",
    "Runner",
    "SourceError",
    "SourceRegistry",
    "SourceSpec",
    "assert_may_collect",
    "catchup",
    "compliance_verdict",
    "detect",
    "detect_all",
    "expected_dates",
    "freshness",
    "is_expected",
    "load_adapter",
    "load_canary",
    "load_extractor",
    "parse_date_arg",
]
