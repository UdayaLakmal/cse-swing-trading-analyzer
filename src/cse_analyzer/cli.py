"""Command line entry point: `cse backfill`, `cse daily`, `cse status`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import collector, db
from .config import load_settings
from .cse_client import PERIOD_YEAR, CseClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cse")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill", help="Fetch ~1 year of daily history for every stock")
    daily = sub.add_parser("daily", help="Daily collection (skips if today already ran)")
    daily.add_argument("--force", action="store_true", help="Run even if today already succeeded")
    sub.add_parser("status", help="Show what is stored and when it last ran")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    conn = db.connect(settings.db_path)

    if args.cmd == "status":
        print(json.dumps(status(conn), indent=2))
        return 0

    if args.cmd == "daily" and not args.force:
        today = collector.now_colombo().date().isoformat()
        if collector.last_ok_run(conn, "daily") == today:
            print(f"daily: already ran for {today}, skipping (use --force to rerun)")
            return 0

    with CseClient(settings.cse_base_url, settings.request_delay_s, settings.http_timeout_s) as client:
        stats = collector.collect_prices(
            conn,
            client,
            kind=args.cmd,
            force_period=PERIOD_YEAR if args.cmd == "backfill" else None,
            progress=lambda m: logging.info(m),
        )
    print(json.dumps({k: v for k, v in stats.items() if k != "failed"} | {"failed": stats["failed"][:20]}, indent=2))
    return 0 if stats["status"] != "failed" else 1


def status(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    latest = collector.latest_price_day(conn)
    return {
        "stocks": q("SELECT COUNT(*) FROM stocks"),
        "price_rows": q("SELECT COUNT(*) FROM prices_daily"),
        "first_day": q("SELECT MIN(day) FROM prices_daily"),
        "latest_day": latest.isoformat() if latest else None,
        "stale": collector.is_stale(conn),
        "index_rows": q("SELECT COUNT(*) FROM index_daily"),
        "announcements": q("SELECT COUNT(*) FROM announcements"),
        "last_runs": [dict(r) for r in conn.execute("SELECT run_day, kind, status, message FROM runs ORDER BY id DESC LIMIT 5")],
    }


if __name__ == "__main__":
    sys.exit(main())
