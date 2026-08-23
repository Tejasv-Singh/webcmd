"""amfi-nav-probe adapter — fetch and persist raw bytes. Nothing else.

Read the whole of this file and notice what is missing: there is no parsing, no split
on semicolons, no float(), no Observation. The adapter's entire job is to get the
bytes onto disk with provenance attached (CLAUDE.md rule 2), so that an extractor
written next year can be run over today's capture.

The temptation to "just pull the NAV out while we're here" is exactly the thing the
architecture forecloses, and `extractor-author` has no network access for the same
reason in reverse.
"""

from __future__ import annotations

from datetime import date

SOURCE_SLUG = "amfi-nav-probe"
NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"


def fetch(ctx, target_date: date | None = None) -> None:
    """Capture today's NAV file.

    One URL, one request, one blob. If the bytes are identical to yesterday's the blob
    store deduplicates them and records a second capture — which is itself a signal
    worth having, because an unchanged NAV file on a business day means AMFI did not
    publish.
    """
    capture = ctx.fetch_and_put(NAV_URL)
    if capture.deduplicated:
        ctx.note(
            "byte-identical to an earlier capture: AMFI has probably not published "
            "yet for this date. The canary will catch it if the file dates are stale."
        )
