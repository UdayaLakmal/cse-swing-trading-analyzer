"""Desktop notification text for the daily run.

`cse summary` prints this as JSON; scripts/cse-daily.sh hands it to
notify-send on the host (the container has no desktop to talk to).
"""

from __future__ import annotations

import sqlite3

from . import analysis, collector, fundamentals

ACTION_VERDICTS = ("Sell", "Trim", "Add")


def _short(symbols: list[str], limit: int = 6) -> str:
    names = [s.split(".")[0] for s in symbols]
    return ", ".join(names[:limit]) + (f" +{len(names) - limit} more" if len(names) > limit else "")


def build_summary(conn: sqlite3.Connection) -> dict:
    """{"title", "body", "urgency"} for the latest analysis. Sell makes it
    critical so it stays on screen until dismissed."""
    day, sigs = analysis.load_signals(conn)
    if not day:
        return {"title": "CSE Analyzer: no analysis yet", "body": "Run a backfill first.", "urgency": "normal"}

    held = [x for x in sigs if x["list"] == "holding"]
    by = {v: sorted(x["symbol"] for x in held if x["verdict"] == v) for v in ACTION_VERDICTS}
    enter_watch = sorted(x["symbol"] for x in sigs if x["list"] == "watch" and x["verdict"] == "Enter")
    enter_scan = sum(1 for x in sigs if x["list"] == "scan" and x["verdict"] == "Enter")

    parts = [f"{len(by[v])} {v}" for v in ACTION_VERDICTS if by[v]]
    if enter_watch:
        parts.append(f"{len(enter_watch)} Enter")
    title = "CSE: " + (" · ".join(parts) if parts else "nothing to act on")

    lines = [f"{v}: {_short(by[v])}" for v in ACTION_VERDICTS if by[v]]
    if enter_watch:
        lines.append(f"Enter (watchlist): {_short(enter_watch)}")
    if held and not any(by.values()):
        lines.append(f"All {len(held)} holdings are Hold")
    xd = fundamentals.upcoming_xd(conn, [x["symbol"] for x in held], within_days=7)
    if xd:
        lines.append("Ex-dividend soon: " + ", ".join(
            f"{s.split('.')[0]} {d['xd'][8:10]}/{d['xd'][5:7]}" for s, d in sorted(xd.items(), key=lambda kv: kv[1]["xd"])))
    regime = analysis.market_regime(conn)
    market = "ASPI uptrend" if regime["ok"] else "ASPI downtrend, buys on hold"
    lines.append(f"{enter_scan} market idea{'s' if enter_scan != 1 else ''} · {market}" if regime.get("known")
                 else f"{enter_scan} market idea{'s' if enter_scan != 1 else ''}")
    lines.append(f"Close of {day}" + (" (prices may be stale)" if collector.is_stale(conn) else ""))
    return {"title": title, "body": "\n".join(lines), "urgency": "critical" if by["Sell"] else "normal"}
