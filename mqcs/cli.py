"""Command-line interface: `python -m mqcs <command>`."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone

from . import db
from .settings import settings


def _date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mqcs", description="Media Quotes Collection System")
    p.add_argument("--no-llm", action="store_true", help="skip model-assisted steps (discovery/fetch only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database and register the configured sources")
    d = sub.add_parser("daily", help="daily run: collect the last few days, then write the 14-day digest")
    d.add_argument("--date", type=_date, help="treat this date as 'today' (default: today UTC)")
    d.add_argument("--no-report", action="store_true")

    b = sub.add_parser("backfill", help="collect quotes for a past period and append them to the database")
    b.add_argument("--from", dest="start", type=_date, default=None, help=f"default {settings.collection_start}")
    b.add_argument("--to", dest="end", type=_date, default=None, help="default: today")
    b.add_argument("--source", action="append", help="limit to source id(s), e.g. bbc")
    b.add_argument("--exhaustive", action="store_true",
                   help="also scan full archive sitemaps (slower; maximises recall)")

    a = sub.add_parser("add-source", help="auto-build a pipeline for a new outlet and backfill it")
    a.add_argument("--name", required=True)
    a.add_argument("--url", required=True, help="homepage, e.g. https://www.theguardian.com/")
    a.add_argument("--from", dest="start", type=_date, default=None)

    r = sub.add_parser("report", help="(re)generate the digest for a rolling window ending on --date")
    r.add_argument("--date", type=_date)
    r.add_argument("--days", type=int, default=None)

    i = sub.add_parser("import-html", help="ingest a manually saved article page (for sources that block bots)")
    i.add_argument("--source", required=True)
    i.add_argument("--url", required=True)
    i.add_argument("--file", required=True)

    sub.add_parser("export", help="rewrite the JSONL/JSON/CSV exports")
    sub.add_parser("audit", help="re-verify every stored quote against its raw page snapshot")
    sub.add_parser("status", help="print database statistics")
    w = sub.add_parser("dashboard", help="run the web dashboard")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8050)

    args = p.parse_args(argv)
    conn = db.connect()
    from . import pipeline
    pipeline.setup(conn)
    use_llm = not args.no_llm
    today = datetime.now(timezone.utc).date()

    if args.cmd == "init":
        print(f"Database ready at {settings.db_path}")
    elif args.cmd == "daily":
        pipeline.daily(conn, args.date or today, use_llm=use_llm, with_report=not args.no_report)
    elif args.cmd == "backfill":
        pipeline.backfill(conn, args.start or settings.collection_start, args.end or today, args.source,
                          exhaustive=args.exhaustive, use_llm=use_llm)
    elif args.cmd == "add-source":
        sid = pipeline.add_source(conn, args.name, args.url, args.start, use_llm=use_llm)
        print(f"Source registered as '{sid}'")
    elif args.cmd == "report":
        from .report import generate_report
        path = generate_report(conn, args.date or today, args.days)
        print(path or "report failed - see logs")
        return 0 if path else 1
    elif args.cmd == "import-html":
        with open(args.file, "rb") as f:
            pipeline.import_html(conn, args.source, args.url, f.read(), use_llm=use_llm)
    elif args.cmd == "export":
        from .export import export_all
        print(json.dumps(export_all(conn), indent=2))
    elif args.cmd == "audit":
        print(json.dumps(pipeline.reverify(conn), indent=2))
    elif args.cmd == "status":
        from .dashboard.app import stats
        print(json.dumps(stats(conn), indent=2, default=str))
    elif args.cmd == "dashboard":
        from .dashboard.app import create_app
        create_app().run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
