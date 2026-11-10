"""Fetches cse.lk data and stores it. Used by the daily job and the backfill."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Callable

from .config import COLOMBO, MARKET_CLOSE_FINAL
from .cse_client import (
    INDEX_CHART_IDS,
    PERIOD_WEEK,
    PERIOD_YEAR,
    Bar,
    CseApiError,
    CseClient,
    IndexPoint,
    StockSummary,
)

log = logging.getLogger(__name__)

CSE_FILE_BASE = "https://cdn.cse.lk/"


def now_colombo() -> datetime:
    return datetime.now(tz=COLOMBO)


def is_final(day: date, now: datetime) -> bool:
    """A bar is final if it is from an earlier day, or today after the close."""
    today = now.date()
    if day < today:
        return True
    if day > today:
        return False
    return (now.hour, now.minute) >= MARKET_CLOSE_FINAL


def final_daily_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    """One bar per final day (the last one seen for that day wins)."""
    by_day: dict[date, Bar] = {}
    for b in bars:
        if b.close is not None and is_final(b.day, now):
            by_day[b.day] = b
    return [by_day[d] for d in sorted(by_day)]


def period_for_gap(last_day: date | None, today: date) -> int:
    """Smallest chart period that covers the gap since the last stored bar."""
    if last_day is None:
        return PERIOD_YEAR
    gap = (today - last_day).days
    if gap <= 5:
        return PERIOD_WEEK
    if gap <= 25:
        return 3
    if gap <= 80:
        return 4
    return PERIOD_YEAR


# --- storage ---------------------------------------------------------------


def upsert_stocks(conn: sqlite3.Connection, stocks: list[StockSummary]) -> None:
    ts = now_colombo().isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO stocks (stock_id, symbol, name, market_cap, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(stock_id) DO UPDATE SET
             symbol=excluded.symbol, name=excluded.name,
             market_cap=excluded.market_cap, updated_at=excluded.updated_at""",
        [(s.stock_id, s.symbol, s.name, s.market_cap, ts) for s in stocks],
    )


def upsert_bars(conn: sqlite3.Connection, stock_id: int, bars: list[Bar]) -> int:
    conn.executemany(
        """INSERT INTO prices_daily (stock_id, day, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(stock_id, day) DO UPDATE SET
             high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume""",
        [(stock_id, b.day.isoformat(), b.high, b.low, b.close, b.volume) for b in bars],
    )
    return len(bars)


def upsert_index(conn: sqlite3.Connection, code: str, points: list[IndexPoint], now: datetime) -> int:
    by_day: dict[date, IndexPoint] = {}
    for p in points:
        if is_final(p.ts.date(), now):
            by_day[p.ts.date()] = p
    conn.executemany(
        """INSERT INTO index_daily (index_code, day, value, change_pct) VALUES (?, ?, ?, ?)
           ON CONFLICT(index_code, day) DO UPDATE SET value=excluded.value, change_pct=excluded.change_pct""",
        [(code, d.isoformat(), p.value, p.change_pct) for d, p in sorted(by_day.items())],
    )
    return len(by_day)


def upsert_announcements(conn: sqlite3.Connection, kind: str, items: list[dict]) -> int:
    rows = []
    for a in items:
        if a.get("id") is None:
            continue
        if kind == "financial":
            title, category, announced = a.get("fileText"), "FINANCIAL", a.get("uploadedDate")
            company, url = a.get("name"), (CSE_FILE_BASE + a["path"]) if a.get("path") else None
        else:
            title = category = a.get("announcementCategory")
            announced, company, url = a.get("dateOfAnnouncement"), a.get("company"), None
        rows.append((a["id"], kind, company, a.get("symbol"), category, title, announced, url, json.dumps(a)))
    conn.executemany(
        """INSERT OR IGNORE INTO announcements
           (id, kind, company, symbol, category, title, announced, url, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return len(rows)


def last_ok_run(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(run_day) FROM runs WHERE kind = ? AND status IN ('ok', 'partial')", (kind,)
    ).fetchone()
    return row[0]


def _start_run(conn: sqlite3.Connection, kind: str, day: date) -> int:
    cur = conn.execute("INSERT INTO runs (run_day, kind, status) VALUES (?, ?, 'running')", (day.isoformat(), kind))
    conn.commit()
    return cur.lastrowid


def _finish_run(conn: sqlite3.Connection, run_id: int, status: str, message: str) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, message = ?, finished_at = datetime('now') WHERE id = ?",
        (status, message, run_id),
    )
    conn.commit()


# --- jobs ------------------------------------------------------------------


def collect_prices(
    conn: sqlite3.Connection,
    client: CseClient,
    *,
    kind: str,
    force_period: int | None = None,
    progress: Callable[[str], None] = lambda _m: None,
) -> dict:
    """Refresh the stock list, every stock's bars, both indices and announcements."""
    now = now_colombo()
    run_id = _start_run(conn, kind, now.date())
    stats = {"stocks": 0, "bars": 0, "index_points": 0, "announcements": 0, "failed": []}
    try:
        stocks = client.trade_summary()
        upsert_stocks(conn, stocks)
        conn.commit()
        stats["stocks"] = len(stocks)

        last_days = {
            r[0]: date.fromisoformat(r[1])
            for r in conn.execute("SELECT stock_id, MAX(day) FROM prices_daily GROUP BY stock_id")
        }
        for i, s in enumerate(stocks, 1):
            period = force_period or period_for_gap(last_days.get(s.stock_id), now.date())
            try:
                bars = final_daily_bars(client.stock_chart(s.stock_id, period), now)
                stats["bars"] += upsert_bars(conn, s.stock_id, bars)
                conn.commit()
            except CseApiError as e:
                log.warning("%s: %s", s.symbol, e)
                stats["failed"].append(s.symbol)
            if i % 25 == 0:
                progress(f"{i}/{len(stocks)} stocks")

        for code in INDEX_CHART_IDS:
            stats["index_points"] += upsert_index(conn, code, client.index_chart(code, PERIOD_YEAR), now)
        stats["announcements"] += upsert_announcements(conn, "approved", client.approved_announcements())
        stats["announcements"] += upsert_announcements(conn, "financial", client.financial_announcements())
        conn.commit()
    except Exception as e:
        _finish_run(conn, run_id, "failed", str(e))
        raise

    # More than 10% of stocks failing means the data is not trustworthy today.
    failed = len(stats["failed"])
    status = "ok" if failed == 0 else ("partial" if failed <= 0.1 * max(stats["stocks"], 1) else "failed")
    _finish_run(conn, run_id, status, json.dumps({k: v for k, v in stats.items() if k != "failed"} | {"failed": failed}))
    stats["status"] = status
    return stats


def latest_price_day(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MAX(day) FROM prices_daily").fetchone()
    return date.fromisoformat(row[0]) if row[0] else None


def is_stale(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """True if the newest stored bar is older than the last weekday before today.

    Ignores CSE holidays, so it can flag a false 'stale' the day after one.
    """
    now = now or now_colombo()
    latest = latest_price_day(conn)
    if latest is None:
        return True
    expected = now.date() - timedelta(days=1)
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    return latest < expected
