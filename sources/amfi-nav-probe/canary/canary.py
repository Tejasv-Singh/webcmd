"""amfi-nav-probe canaries — written from BRIEF.md, not from the extractor.

That separation is the point. If these assertions were derived from reading
`extractor.py`, they would encode the same misunderstanding the extractor has and pass
happily while the corpus fills with rubbish. Written from the brief, they check what
the *file* is supposed to look like.

Every threshold below traces to a line in BRIEF.md under "Canary expectations", and
every change to one is recorded in the CALIBRATION LOG at the bottom of this file. A
threshold quietly loosened during an incident is worse than no threshold at all.
"""

from __future__ import annotations

import os
from collections import Counter

from core.monitoring.checks import CanaryContext, CanaryResult

# Read per run rather than at import: source modules are cached for the life of the
# process, and a threshold frozen at import cannot be changed without a restart --
# which is exactly the wrong property for the knob you reach for during an incident.
DEFAULTS = {
    "KOSH_AMFI_MIN_SCHEMES": "5000",
    "KOSH_AMFI_MIN_CURRENT_DAY_ROWS": "3000",
    "KOSH_AMFI_MAX_NAV": "10000000",
    "KOSH_AMFI_MAX_DATE_LAG_DAYS": "5",
}


def _threshold(name: str) -> float:
    return float(os.environ.get(name, DEFAULTS[name]))


# Volume drift override for this source. Priced-row counts are extremely stable -- the
# scheme universe moves by a handful a week -- so a tighter band than the global
# default is appropriate here.
DRIFT = {"threshold": 0.1, "window": 14}


def run(ctx: CanaryContext) -> list[CanaryResult]:
    min_schemes = int(_threshold("KOSH_AMFI_MIN_SCHEMES"))
    min_current = int(_threshold("KOSH_AMFI_MIN_CURRENT_DAY_ROWS"))
    max_nav = _threshold("KOSH_AMFI_MAX_NAV")
    max_lag = int(_threshold("KOSH_AMFI_MAX_DATE_LAG_DAYS"))

    results: list[CanaryResult] = []
    navs = ctx.metric_values("nav.close")

    # 1. Volume. The failure this catches is the silent one: a layout change that makes
    #    the parser drop every row while everything else reports success. That exact
    #    thing happened on the first live capture -- see the calibration log.
    if not navs:
        return [
            ctx.fail(
                "scheme-count",
                f"zero NAV observations for {ctx.target_date}. Per CLAUDE.md rule 4 "
                "this is P0 until the source itself has been checked.",
            )
        ]
    if len(navs) < min_schemes:
        results.append(
            ctx.fail(
                "scheme-count",
                f"{len(navs)} priced schemes, expected at least {min_schemes}. The file "
                "was truncated or the parse is dropping rows.",
            )
        )
    else:
        results.append(ctx.ok("scheme-count", f"{len(navs)} priced schemes"))

    # 2. Value sanity. A NAV cannot be negative or zero, and one above the ceiling is a
    #    decimal-separator bug rather than a very good fund.
    bad = [v for v in navs if v is None or v <= 0 or v > max_nav]
    if bad:
        results.append(
            ctx.fail("nav-range", f"{len(bad)} NAV(s) outside (0, {max_nav:,.0f}]: {bad[:5]}")
        )
    else:
        results.append(ctx.ok("nav-range", f"min {min(navs):.4f}, max {max(navs):,.4f}"))

    # 3. Entity keys are ISINs. Anything else means something resolved by name.
    rows = ctx.observations(metric="nav.close")
    non_isin = [r["entity_id"] for r in rows if not r["entity_id"].startswith("ISIN:")]
    if non_isin:
        results.append(
            ctx.fail(
                "isin-keys",
                f"{len(non_isin)} observation(s) not keyed on ISIN: {non_isin[:5]}",
            )
        )
    else:
        results.append(ctx.ok("isin-keys", "all observations ISIN-keyed"))

    if rows:
        dates = Counter(r["observed_at"].date() for r in rows)
        modal_date, modal_count = dates.most_common(1)[0]
        lag = (ctx.target_date - modal_date).days

        # 4. Freshness, measured on the MODAL row date. The file is a last-known-NAV
        #    table covering every scheme that ever existed, so its maximum date is a
        #    forward-dated outlier and its minimum is 2012 -- neither says anything
        #    about whether AMFI published today. The mode does.
        if lag > max_lag:
            results.append(
                ctx.fail(
                    "modal-date-freshness",
                    f"the most common NAV date in the file is {modal_date}, {lag} days "
                    f"before the collection date {ctx.target_date}. AMFI is serving a "
                    "stale file or collection is running against the wrong date.",
                )
            )
        elif lag > 1:
            results.append(
                ctx.warn(
                    "modal-date-freshness", f"modal NAV date {modal_date} lags by {lag} days"
                )
            )
        else:
            results.append(ctx.ok("modal-date-freshness", f"modal NAV date {modal_date}"))

        # 5. And the mode has to be a real majority, not three schemes agreeing. This
        #    catches AMFI publishing a near-empty file for the current day while the
        #    historic tail keeps the total row count looking perfectly healthy.
        if modal_count < min_current:
            results.append(
                ctx.fail(
                    "current-day-volume",
                    f"only {modal_count} rows carry the modal date {modal_date}, "
                    f"expected at least {min_current}. The current day is barely "
                    "populated even though the file as a whole looks normal.",
                )
            )
        else:
            results.append(
                ctx.ok("current-day-volume", f"{modal_count} rows dated {modal_date}")
            )

    # 6. One value per instrument per day. Two different NAVs for the same ISIN on the
    #    same date from one capture means the file or the parse is double-counting.
    per_key: dict[tuple, set] = {}
    for r in rows:
        per_key.setdefault((r["entity_id"], r["observed_at"]), set()).add(r["value"])
    dupes = [k for k, v in per_key.items() if len(v) > 1]
    if dupes:
        results.append(
            ctx.fail(
                "no-conflicting-duplicates",
                f"{len(dupes)} ISIN/date pair(s) carry conflicting NAVs, e.g. {dupes[0]}",
            )
        )
    else:
        results.append(ctx.ok("no-conflicting-duplicates", f"{len(per_key)} distinct keys"))

    return results


# ---------------------------------------------------------------------------
# CALIBRATION LOG
#
# Thresholds change only here, only deliberately, and only with a reason. If you are
# reading this during an incident and want to loosen something, write the line first.
#
# 2026-08-22  MAX_NAV 1,000,000 -> 10,000,000.
#             Twelve rows tripped the old ceiling on the first live capture. They are
#             the IL&FS Infrastructure Debt Fund series, priced above 25 lakh per unit
#             because the units were issued at a high face value. Real data, wrong
#             threshold -- verified by tracing the outliers back to the raw blob.
#
# 2026-08-22  date-freshness (max row date) -> modal-date-freshness (modal row date),
#             plus a new current-day-volume check.
#             The original check assumed the file was a daily snapshot. It is not: it
#             is a last-known-NAV table spanning 2012 to today, so the maximum date was
#             a forward-dated Sunday outlier and told us nothing. The mode is the real
#             signal, and pairing it with a minimum row count closes the gap where the
#             historic tail alone would keep the total looking healthy.
# ---------------------------------------------------------------------------
