"""`kosh` — the operator CLI.

The commands you will actually type, day to day:

    kosh collect --source <slug> --date <YYYY-MM-DD>    one source, one date
    kosh extract --source <slug> --date <YYYY-MM-DD>    raw -> observations
    kosh run     --source <slug> --date today           both, plus canaries
    kosh health                                         every morning
    kosh gaps --source <slug> / kosh catchup --source <slug>
    kosh backfill diff --source <slug>                  before you commit a backfill

Keep the two phases separate in your head as well as in the code: `collect` touches
the network and never parses, `extract` parses and never touches the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.config import REPO_ROOT, settings
from core.entities.graph import EntityGraph, parse_nse_equity_list, seed_from_csv
from core.extract.backfill import diff_against_stored, run_backfill, sample_captures
from core.monitoring.checks import health as build_health
from core.monitoring.checks import last_canary_fire, run_canaries
from core.scheduler.gaps import catchup as run_catchup
from core.scheduler.gaps import detect
from core.scheduler.registry import (
    SourceRegistry,
    compliance_verdict,
    parse_date_arg,
)
from core.scheduler.runner import Runner
from core.store.blobs import BlobStore
from core.store.db import connect
from core.store.observations import ObservationStore
from core.timeutil import fmt_ist

NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _db(args: argparse.Namespace, *, migrate: bool = False):
    return connect(getattr(args, "database_url", None), migrate=migrate)


def _print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("  (nothing)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  " + "  ".join(c.ljust(widths[c]) for c in columns))
    print("  " + "  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def _report_run(result: Any) -> int:
    icon = {"ok": "ok", "empty": "EMPTY", "error": "ERROR"}.get(result.status, result.status)
    print(
        f"  [{icon}] {result.source_slug} {result.phase} "
        f"{result.target_date or '(no date)'} — blobs={result.blob_count} "
        f"rows={result.row_count}"
    )
    for note in result.notes:
        print(f"         note: {note}")
    if result.error:
        print(f"         {result.error.splitlines()[0]}")
        if result.status == "error":
            print("         " + "\n         ".join(result.error.splitlines()[1:6]))
    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_migrate(args: argparse.Namespace) -> int:
    db = _db(args)
    applied = db.migrate(verbose=True)
    print(f"  {db.dialect}: {len(applied)} migration(s) applied, schema up to date")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    runner = Runner(db)
    target = parse_date_arg(args.date)
    return _report_run(
        runner.collect(args.source, target, skip_compliance=args.skip_compliance)
    )


def cmd_extract(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    runner = Runner(db)
    return _report_run(
        runner.extract(
            args.source,
            parse_date_arg(args.date),
            since=parse_date_arg(args.since),
            until=parse_date_arg(args.until),
        )
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Collect, extract and check, in one command. What cron calls."""
    db = _db(args, migrate=True)
    runner = Runner(db)
    target = parse_date_arg(args.date)
    captured = runner.collect(args.source, target, skip_compliance=args.skip_compliance)
    rc = _report_run(captured)
    if not captured.ok:
        return rc
    rc |= _report_run(runner.extract(args.source, target))
    spec = SourceRegistry(db).get(args.source)
    if spec:
        findings = run_canaries(db, spec, target)
        for f in findings:
            print(f"  {f}")
        if any(f.severity == "P0" for f in findings):
            rc = 1
    return rc


def cmd_sources(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    reg = SourceRegistry(db)

    if args.action == "list":
        rows = []
        for spec in reg.all():
            rows.append(
                {
                    "slug": spec.slug,
                    "ring": spec.ring,
                    "cadence": spec.cadence,
                    "calendar": spec.calendar,
                    "enabled": "yes" if spec.enabled else "no",
                    "compliance": spec.compliance_verdict or "MISSING",
                }
            )
        _print_table(rows, ["slug", "ring", "cadence", "calendar", "enabled", "compliance"])
        unregistered = [s for s in reg.discover() if not reg.get(s)]
        if unregistered:
            print(f"\n  on disk but not registered: {', '.join(unregistered)}")
        return 0

    if args.action == "register":
        if not args.slug:
            print("  --slug is required", file=sys.stderr)
            return 2
        verdict = compliance_verdict(args.slug)
        if verdict is None:
            print(
                f"  refusing: sources/{args.slug}/COMPLIANCE.md is missing or has no "
                "VERDICT line. Compliance review comes first (CLAUDE.md).",
                file=sys.stderr,
            )
            return 1
        if verdict == "STOP":
            print(f"  refusing: compliance verdict for {args.slug} is STOP.", file=sys.stderr)
            return 1
        spec = reg.register(
            slug=args.slug,
            display_name=args.name or args.slug,
            url=args.url or "",
            ring=args.ring,
            cadence=args.cadence,
            calendar=args.calendar,
            enabled=args.enable,
        )
        db.commit()
        print(f"  registered {spec.slug} (compliance: {verdict}, enabled: {spec.enabled})")
        return 0

    if args.action in ("enable", "disable"):
        if not args.slug:
            print("  --slug is required", file=sys.stderr)
            return 2
        reg.set_enabled(args.slug, args.action == "enable")
        db.commit()
        print(f"  {args.slug} {args.action}d")
        return 0
    return 2


def cmd_gaps(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    reg = SourceRegistry(db)
    specs = [reg.get(args.source)] if args.source else reg.all(enabled_only=not args.all)
    rc = 0
    for spec in specs:
        if spec is None:
            print(f"  no such source: {args.source}", file=sys.stderr)
            return 1
        report = detect(db, spec, since=parse_date_arg(args.since))
        print(f"  {report.summary()}")
        for w in report.warnings:
            print(f"    warning: {w}")
        if report.missing:
            rc = 1
            shown = [str(d) for d in report.missing[:20]]
            print(f"    missing: {', '.join(shown)}")
            if len(report.missing) > 20:
                print(f"    ... and {len(report.missing) - 20} more")
    return rc


def cmd_catchup(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    runner = Runner(db)
    results = run_catchup(
        runner, args.source, max_days=args.max_days, extract=not args.no_extract
    )
    if not results:
        print("  no gaps to fill")
        return 0
    for r in results:
        label = r["date"] or "-"
        print(
            f"  {label}: capture={r['capture']} blobs={r['blobs']} "
            f"extract={r.get('extract', '-')} rows={r.get('rows', 0)}"
        )
        if r.get("error"):
            print(f"    {r['error'].splitlines()[0]}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    report = build_health(db, day=parse_date_arg(args.date), run_canary=not args.no_canary)

    print(f"Kosh health — {fmt_ist(report.generated_at)}")
    print("=" * 60)
    _print_table(
        [
            {
                "slug": r["slug"],
                "ring": r["ring"],
                "last capture": fmt_ist(r["last_capture"]),
                "age (h)": r["age_hours"] if r["age_hours"] is not None else "-",
                "coverage": r["coverage"],
            }
            for r in report.sources
        ],
        ["slug", "ring", "last capture", "age (h)", "coverage"],
    )
    print()
    if not report.findings:
        print("  no findings. Collection is healthy.")
    for severity in ("P0", "P1", "P2", "INFO"):
        group = report.by_severity(severity)
        if group:
            print(f"  {severity} ({len(group)})")
            for f in group:
                print(f"    {f.slug}: {f.message}")
    print()
    print(f"  worst severity: {report.worst}")
    if report.by_severity("P0"):
        print(
            "\n  A P0 is not resolved by deciding it was probably a quiet day. Check the\n"
            "  source itself, then run `kosh catchup --source <slug>` while the source\n"
            "  still serves that date."
        )
    return 1 if report.by_severity("P0") else 0


def cmd_canary(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    spec = SourceRegistry(db).get(args.source)
    if spec is None:
        print(f"  no such source: {args.source}", file=sys.stderr)
        return 1
    findings = run_canaries(db, spec, parse_date_arg(args.date))
    # Timestamped, because this table is a history: a failure from an earlier run
    # sitting above a later pass is easy to misread as a current failure.
    rows = db.query(
        "SELECT created_at, check_name, status, detail FROM canary_results"
        " WHERE source_slug = ? ORDER BY id DESC LIMIT 20",
        (args.source,),
    )
    for r in rows:
        r["when"] = fmt_ist(r.pop("created_at"))
        r["detail"] = (r["detail"] or "")[:70]
    _print_table(rows, ["when", "check_name", "status", "detail"])
    for f in findings:
        print(f"  {f}")
    return 1 if any(f.severity == "P0" for f in findings) else 0


def cmd_backfill(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)

    if args.action == "sample":
        ids = sample_captures(db, args.source, args.n)
        print(f"  {len(ids)} capture(s) sampled across time:")
        for cid in ids:
            cap = BlobStore(db).get_capture(cid)
            print(f"    {cid[:12]}  {cap['target_date'] if cap else '?'}")
        return 0

    if args.action == "diff":
        ids = sample_captures(db, args.source, args.n)
        if not ids:
            print("  no captures to diff against")
            return 1
        diff = diff_against_stored(db, args.source, ids, old_ver=args.old_version)
        print(f"  {diff.summary()}")
        for w in diff.is_suspicious():
            print(f"    WARNING: {w}")
        for row in diff.changed[:10]:
            print(f"    changed {row['key']}: {row['old']!r} -> {row['new']!r}")
        for row in diff.added[:10]:
            print(f"    added   {row['key']}: {row['new']!r}")
        for row in diff.removed[:10]:
            print(f"    removed {row['key']}: {row['old']!r}")
        print(
            "\n  Investigate every difference before committing. Then:\n"
            f"    kosh backfill run --source {args.source}"
        )
        return 0

    if args.action == "run":
        if not args.yes:
            print(
                "  Refusing to run a backfill without --yes.\n"
                "  The store is append-only: a bad backfill cannot be undone, only\n"
                "  superseded. Run `kosh backfill diff` first and read the output."
            )
            return 2
        result = run_backfill(
            db,
            args.source,
            since=parse_date_arg(args.since),
            until=parse_date_arg(args.until),
            limit=args.limit,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] in ("ok", "nothing-to-do") else 1
    return 2


def cmd_entities(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    graph = EntityGraph(db)

    if args.action == "stats":
        for k, v in graph.stats().items():
            print(f"  {k}: {v}")
        return 0

    if args.action == "seed":
        path = (
            Path(args.csv) if args.csv else REPO_ROOT / "core/entities/seed/universe.sample.csv"
        )
        if not path.exists():
            print(f"  no such file: {path}", file=sys.stderr)
            return 1
        if "sample" in path.name and not args.yes:
            print(
                f"  {path.name} contains fake test entities (INE000TEST*). Loading it "
                "into a real store would poison the graph. Pass --yes if you mean it."
            )
            return 2
        result = seed_from_csv(graph, path)
        db.commit()
        print(f"  seeded from {path.name}: {result}")
        return 0

    if args.action == "seed-nse":
        from core.scheduler.fetch import Fetcher

        print(f"  fetching {NSE_EQUITY_LIST_URL}")
        try:
            resp = Fetcher().get(NSE_EQUITY_LIST_URL)
        except Exception as exc:
            print(
                f"  fetch failed: {exc}\n"
                "  NSE often refuses plain HTTP clients. Download EQUITY_L.csv in a\n"
                "  browser (or via webcmd) and load it with:\n"
                "    kosh entities seed --csv <path>  (columns: isin,name,nse_symbol)",
                file=sys.stderr,
            )
            return 1
        rows = parse_nse_equity_list(resp.body)
        added = 0
        for row in rows:
            eid = graph.upsert_entity(isin=row["isin"] or None, primary_name=row["name"])
            if graph.add_alias(eid, "nse_symbol", row["nse_symbol"], source="nse-equity-list"):
                added += 1
        db.commit()
        print(f"  seeded {len(rows)} securities, {added} new NSE symbol aliases")
        print(f"  {graph.stats()}")
        return 0

    if args.action == "resolve":
        hit = graph.resolve(args.alias, kind=args.kind, as_of=parse_date_arg(args.as_of))
        if hit is None:
            print(f"  {args.alias!r} does not resolve. Not guessing — that is the point.")
            return 1
        print(
            f"  {args.alias!r} -> {hit.entity_id} ({hit.method}, confidence {hit.confidence})"
        )
        return 0

    if args.action == "queue":
        rows = graph.queue()
        _print_table(
            [
                {
                    "id": r["queue_id"],
                    "kind": r["alias_kind"],
                    "alias": r["alias_raw"],
                    "seen": r["occurrences"],
                    "source": r["source_slug"] or "-",
                }
                for r in rows
            ],
            ["id", "kind", "alias", "seen", "source"],
        )
        if rows:
            print("\n  resolve with: kosh entities link --queue-id <id> --entity <entity_id>")
        return 0

    if args.action == "link":
        graph.resolve_queue_item(args.queue_id, args.entity)
        db.commit()
        print(f"  queue item {args.queue_id} -> {args.entity}")
        return 0
    return 2


def cmd_stats(args: argparse.Namespace) -> int:
    db = _db(args, migrate=True)
    blobs = BlobStore(db).stats()
    obs = ObservationStore(db)
    print("  blob store")
    for k, v in blobs.items():
        print(f"    {k}: {v}")
    print("  observations")
    print(f"    total: {db.scalar('SELECT COUNT(*) FROM observations') or 0}")
    print(
        f"    entities: {db.scalar('SELECT COUNT(DISTINCT entity_id) FROM observations') or 0}"
    )
    print(f"    metrics: {db.scalar('SELECT COUNT(DISTINCT metric) FROM observations') or 0}")
    for row in obs.versions():
        print(f"    {row['extractor_ver']}: {row['n']} rows")
    print("  entity graph")
    for k, v in EntityGraph(db).stats().items():
        print(f"    {k}: {v}")
    fired = last_canary_fire(db)
    print(
        f"  last canary failure: {fmt_ist(fired['created_at']) if fired else 'never'}"
        + ("" if fired else "  <- a canary that has never fired is an untested assertion")
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from core.verify import run_verification, summarise_for_operator

    report = run_verification(live=args.live, keep=args.keep)
    print(report.render())
    if report.passed:
        print("\nNext:")
        print(summarise_for_operator(settings().database_url))
        print(
            "\nThen the first real Ring 1 source:\n"
            "    /new-source nse-announcements "
            "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
        )
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kosh", description=__doc__.split("\n")[0])
    p.add_argument("--database-url", help="override KOSH_DATABASE_URL")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply schema migrations").set_defaults(func=cmd_migrate)

    c = sub.add_parser("collect", help="capture raw bytes for one source and date")
    c.add_argument("--source", required=True)
    c.add_argument("--date", default="today", help="YYYY-MM-DD, 'today' or 'yesterday' (IST)")
    c.add_argument("--skip-compliance", action="store_true", help=argparse.SUPPRESS)
    c.set_defaults(func=cmd_collect)

    e = sub.add_parser("extract", help="turn stored captures into observations")
    e.add_argument("--source", required=True)
    e.add_argument("--date", default=None)
    e.add_argument("--since", default=None)
    e.add_argument("--until", default=None)
    e.set_defaults(func=cmd_extract)

    r = sub.add_parser("run", help="collect + extract + canaries (what cron calls)")
    r.add_argument("--source", required=True)
    r.add_argument("--date", default="today")
    r.add_argument("--skip-compliance", action="store_true", help=argparse.SUPPRESS)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("sources", help="registry")
    s.add_argument("action", choices=["list", "register", "enable", "disable"])
    s.add_argument("--slug")
    s.add_argument("--name")
    s.add_argument("--url")
    s.add_argument("--ring", type=int, default=1)
    s.add_argument("--cadence", default="daily")
    s.add_argument("--calendar", default="weekday", choices=["daily", "weekday", "nse_trading"])
    s.add_argument("--enable", action="store_true")
    s.set_defaults(func=cmd_sources)

    g = sub.add_parser("gaps", help="which expected days have no capture")
    g.add_argument("--source")
    g.add_argument("--since")
    g.add_argument("--all", action="store_true", help="include disabled sources")
    g.set_defaults(func=cmd_gaps)

    cu = sub.add_parser("catchup", help="fill detected gaps, oldest first")
    cu.add_argument("--source", required=True)
    cu.add_argument("--max-days", type=int, default=None)
    cu.add_argument("--no-extract", action="store_true")
    cu.set_defaults(func=cmd_catchup)

    h = sub.add_parser("health", help="morning collection health check")
    h.add_argument("--date", default=None)
    h.add_argument("--no-canary", action="store_true")
    h.set_defaults(func=cmd_health)

    cn = sub.add_parser("canary", help="run one source's canaries now")
    cn.add_argument("--source", required=True)
    cn.add_argument("--date", default=None)
    cn.set_defaults(func=cmd_canary)

    b = sub.add_parser("backfill", help="re-extract archived captures")
    b.add_argument("action", choices=["sample", "diff", "run"])
    b.add_argument("--source", required=True)
    b.add_argument("-n", type=int, default=20, help="sample size")
    b.add_argument("--old-version")
    b.add_argument("--since")
    b.add_argument("--until")
    b.add_argument("--limit", type=int)
    b.add_argument("--yes", action="store_true", help="required for 'run'")
    b.set_defaults(func=cmd_backfill)

    en = sub.add_parser("entities", help="the ISIN graph")
    en.add_argument("action", choices=["stats", "seed", "seed-nse", "resolve", "queue", "link"])
    en.add_argument("alias", nargs="?")
    en.add_argument("--kind")
    en.add_argument("--as-of")
    en.add_argument("--csv")
    en.add_argument("--queue-id", type=int)
    en.add_argument("--entity")
    en.add_argument("--yes", action="store_true")
    en.set_defaults(func=cmd_entities)

    sub.add_parser("stats", help="what the corpus contains").set_defaults(func=cmd_stats)

    v = sub.add_parser("verify-foundation", help="the six foundation checks")
    v.add_argument("--live", action="store_true", help="fetch AMFI for real")
    v.add_argument("--keep", action="store_true", help="keep the throwaway store")
    v.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
