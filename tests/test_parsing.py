import json
from datetime import date, datetime
from pathlib import Path

from cse_analyzer import collector, db
from cse_analyzer.config import COLOMBO
from cse_analyzer.cse_client import parse_stock_chart, parse_trade_summary

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_trade_summary_parses_ids_and_symbols():
    stocks = parse_trade_summary(load("tradeSummary.json"))
    jkh = next(s for s in stocks if s.symbol == "JKH.N0000")
    assert jkh.stock_id == 297
    assert jkh.name == "JOHN KEELLS HOLDINGS PLC"


def test_chart_bars_have_colombo_dates():
    bars = parse_stock_chart(load("chart_jkh_period5.json"))
    assert bars[0].day == date(2025, 11, 11)
    assert bars[0].close == 22.4 and bars[0].volume == 10861997


def test_todays_partial_bar_is_dropped_before_close():
    bars = parse_stock_chart(load("chart_jkh_period5.json"))
    morning = datetime(2026, 11, 10, 10, 0, tzinfo=COLOMBO)
    evening = datetime(2026, 11, 10, 15, 0, tzinfo=COLOMBO)
    assert collector.final_daily_bars(bars, morning)[-1].day == date(2026, 11, 9)
    assert collector.final_daily_bars(bars, evening)[-1].day == date(2026, 11, 10)


def test_period_for_gap():
    today = date(2026, 11, 10)
    assert collector.period_for_gap(None, today) == 5
    assert collector.period_for_gap(date(2026, 11, 7), today) == 2
    assert collector.period_for_gap(date(2026, 10, 25), today) == 3
    assert collector.period_for_gap(date(2026, 1, 1), today) == 5


def test_store_and_stale(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    stocks = parse_trade_summary(load("tradeSummary.json"))
    collector.upsert_stocks(conn, stocks)
    bars = parse_stock_chart(load("chart_jkh_period5.json"))
    now = datetime(2026, 11, 10, 10, 0, tzinfo=COLOMBO)
    collector.upsert_bars(conn, 297, collector.final_daily_bars(bars, now))
    # Upserting twice must not duplicate rows.
    n = collector.upsert_bars(conn, 297, collector.final_daily_bars(bars, now))
    assert conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0] == n
    assert not collector.is_stale(conn, now)
    assert collector.is_stale(conn, datetime(2026, 11, 13, 10, 0, tzinfo=COLOMBO))
    assert conn.execute("SELECT value FROM settings WHERE key='stop_loss_pct'").fetchone()[0] == "10"
