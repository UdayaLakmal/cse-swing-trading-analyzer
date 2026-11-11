import math
from datetime import date, timedelta

import pandas as pd
import pytest

from cse_analyzer import analysis, db
from cse_analyzer import indicators as ind
from cse_analyzer.portfolio import Holding
from cse_analyzer.trades import TradeRow, save_trades

SETTINGS = {k: float(v) for k, v in db.DEFAULT_SETTINGS.items()}


def frame(closes, volume=1_000_000, start=date(2025, 11, 1)):
    days = [(start + timedelta(days=i)).isoformat() for i in range(len(closes))]
    vols = volume if isinstance(volume, list) else [volume] * len(closes)
    return pd.DataFrame(
        {"high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes], "close": closes, "volume": vols},
        index=days,
    )


def uptrend(n=250, start=10.0, step=0.004):
    # steady rise with a gentle wobble so RSI is not pinned at 100
    return [start * (1 + step) ** i * (1 + 0.01 * math.sin(i / 3)) for i in range(n)]


# --- indicators ------------------------------------------------------------


def test_rsi_bounds_and_direction():
    up = ind.rsi(pd.Series(uptrend()))
    down = ind.rsi(pd.Series(list(reversed(uptrend()))))
    assert 50 < up.iloc[-1] <= 100
    assert 0 <= down.iloc[-1] < 50
    assert ind.rsi(pd.Series([float(i) for i in range(1, 40)])).iloc[-1] == 100


def test_atr_matches_constant_range():
    df = frame([100.0] * 60)
    assert ind.atr(df).iloc[-1] == pytest.approx(2.0, rel=1e-3)  # high-low = 1% each side


def test_macd_positive_in_uptrend():
    assert ind.macd(pd.Series(uptrend()))["macd"].iloc[-1] > 0


# --- features and score -----------------------------------------------------


def test_uptrend_scores_high_downtrend_low():
    aspi = pd.Series([1000.0] * 250, index=frame(uptrend()).index)
    fu = analysis.compute_features("UP.N0000", "Up", frame(uptrend()), aspi)
    fd = analysis.compute_features("DN.N0000", "Down", frame(list(reversed(uptrend()))), aspi)
    su, cu = analysis.score(fu)
    sd, _ = analysis.score(fd)
    assert su >= 50 and sd <= -50
    assert any("200-day" in c.reason for c in cu)
    assert fu.ma200 is not None and fu.rs3m > 0


def test_short_history_is_skipped_or_noted():
    assert analysis.compute_features("X", "X", frame([10.0] * 20), None) is None
    f = analysis.compute_features("X", "X", frame(uptrend(60)), None)
    assert f.ma200 is None
    _, comps = analysis.score(f)
    assert any("no 200-day average" in c.reason for c in comps)


# --- stops and verdicts ------------------------------------------------------


def holding(entry=100.0, qty=100, stop_price=None, weight=10.0):
    h = Holding(symbol="T.N0000", name="T", quantity=qty, cost=entry * qty * 1.0112, entry_price=entry,
                first_day="2026-01-01", last_close=None, stop_loss_pct=10, stop_price=stop_price, fee_rate=0.0112)
    h.weight_pct = weight
    return h


def feats(close, atr=2.0, **kw):
    base = dict(symbol="T.N0000", name="T", day="2026-11-10", bars=250, close=close, prev_close=close,
                change_pct=0.0, ma20=None, ma50=None, ma200=None, rsi=50.0, macd_hist=0.1, macd_cross=0,
                atr=atr, vol_ratio=1.2, turnover20=5e6, hi52=close * 1.1, lo52=close * 0.7,
                swing_hi20=None, swing_lo20=None, rs1m=None, rs3m=None)
    base.update(kw)
    return analysis.Features(**base)


def test_trailing_stop_stages():
    h = holding()
    assert analysis.trailing_stop(h, 105, 2, SETTINGS) == (pytest.approx(90.0), "initial")
    # +1R (110) reached: at least break-even plus fees
    stop, stage = analysis.trailing_stop(h, 110, 10, SETTINGS)
    assert stage == "break-even" and stop == pytest.approx(h.break_even)
    # well above: Chandelier trail = highest - 3 ATR
    stop, stage = analysis.trailing_stop(h, 130, 2, SETTINGS)
    assert stage == "trailing" and stop == pytest.approx(124.0)
    # a higher stored stop always wins (ratchet)
    assert analysis.trailing_stop(holding(stop_price=126), 130, 2, SETTINGS)[0] == pytest.approx(126)


def test_holding_verdicts():
    kw = dict(sold_since_entry=False, regime_ok=True)
    assert analysis.holding_verdict(holding(), feats(85), 0, SETTINGS, highest=101, **kw).verdict == "Sell"
    assert analysis.holding_verdict(holding(), feats(121), 0, SETTINGS, highest=121, **kw).verdict == "Trim"
    # already took partial profit -> no second Trim at +2R
    v = analysis.holding_verdict(holding(), feats(121), 0, SETTINGS, highest=121, sold_since_entry=True, regime_ok=True)
    assert v.verdict == "Hold"
    assert analysis.holding_verdict(holding(weight=35), feats(102), 0, SETTINGS, highest=102, **kw).verdict == "Trim"
    assert analysis.holding_verdict(holding(), feats(105), 60, SETTINGS, highest=105, **kw).verdict == "Add"
    assert analysis.holding_verdict(holding(), feats(105), 60, SETTINGS, highest=105, sold_since_entry=False,
                                    regime_ok=False).verdict == "Hold"


def test_entry_verdicts():
    good = dict(ma50=95.0, ma200=90.0, rsi=50.0, vol_ratio=1.3)
    v = analysis.entry_verdict(feats(100, **good), 30, SETTINGS, regime_ok=True, capital=1_000_000)
    assert v.verdict == "Enter"
    assert v.stop == pytest.approx(90) and v.target == pytest.approx(120)
    # 1% of 1M = 10k risk / 10 per share = 1000 shares, capped at 20% weight = 2000 -> 1000
    assert v.size_shares == 1000
    assert analysis.entry_verdict(feats(100, **good), 30, SETTINGS, regime_ok=False, capital=0).verdict == "Wait"
    assert analysis.entry_verdict(feats(100, **(good | {"turnover20": 1e5})), 30, SETTINGS, regime_ok=True, capital=0).verdict == "Avoid"
    assert analysis.entry_verdict(feats(80, ma50=85.0, ma200=90.0, rsi=50.0), 0, SETTINGS, regime_ok=True, capital=0).verdict == "Avoid"
    w = analysis.entry_verdict(feats(100, **(good | {"rsi": 72.0})), 30, SETTINGS, regime_ok=True, capital=0)
    assert w.verdict == "Wait" and "RSI 40-60" in w.reasons[0]


# --- end to end --------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    series = {"UP.N0000": uptrend(), "DN.N0000": list(reversed(uptrend())), "FLAT.N0000": [10.0] * 250}
    days = frame(uptrend()).index
    for i, (sym, closes) in enumerate(series.items(), 1):
        c.execute("INSERT INTO stocks VALUES (?, ?, ?, NULL, '')", (i, sym, sym.split('.')[0] + " PLC"))
        c.executemany("INSERT INTO prices_daily VALUES (?, ?, ?, ?, ?, ?)",
                      [(i, d, v * 1.01, v * 0.99, v, 1_000_000) for d, v in zip(days, closes)])
    c.executemany("INSERT INTO index_daily VALUES ('ASPI', ?, ?, NULL)", [(d, 1000 + n) for n, d in enumerate(days)])
    c.commit()
    return c


def test_run_analysis_end_to_end(conn):
    days = frame(uptrend()).index
    save_trades(conn, [TradeRow(date.fromisoformat(days[100]), "UP.N0000", "BUY", 100, uptrend()[100])], "form")
    conn.execute("INSERT INTO watchlist (symbol) VALUES ('DN.N0000')")
    conn.commit()
    r = analysis.run_analysis(conn)
    assert r["analysed"] == 3 and r["day"] == days[-1] and r["regime_ok"] is True
    day, sigs = analysis.load_signals(conn)
    by = {s["symbol"]: s for s in sigs}
    assert by["UP.N0000"]["list"] == "holding" and by["UP.N0000"]["stop_stage"] in ("break-even", "trailing")
    assert by["DN.N0000"]["list"] == "watch" and by["DN.N0000"]["verdict"] == "Avoid"
    assert by["FLAT.N0000"]["list"] == "scan"
    # the trailing stop was stored and does not move down on a re-run
    stored = conn.execute("SELECT stop_price, stop_entry_day FROM position_settings WHERE symbol = 'UP.N0000'").fetchone()
    assert stored["stop_price"] == pytest.approx(by["UP.N0000"]["stop"]) and stored["stop_entry_day"] == days[100]
    analysis.run_analysis(conn)
    assert analysis.load_signals(conn)[1] and conn.execute(
        "SELECT stop_price FROM position_settings WHERE symbol='UP.N0000'").fetchone()[0] >= stored["stop_price"]


def test_pages_render(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CSE_DB_PATH", str(tmp_path / "t.db"))
    from cse_analyzer.web import app

    c = TestClient(app)
    days = frame(uptrend()).index
    save_trades(conn, [TradeRow(date.fromisoformat(days[100]), "UP.N0000", "BUY", 100, uptrend()[100])], "form")
    assert "No analysis yet" in c.get("/").text
    assert "Analysis refreshed" in c.post("/analysis/run").text

    today = c.get("/").text
    assert "Needs your attention" in today and "UP</a>" in today and "Market uptrend" in today
    stock = c.get("/stock/UP.N0000").text
    assert "<svg" in stock and "Why the score is" in stock and "Your position" in stock
    assert c.get("/stock/NOPE.N0000").status_code == 404
    assert c.get("/go?q=up").url.path == "/stock/UP.N0000"
    assert "FLAT" in c.get("/scan?v=All").text
    assert "DN.N0000 is on your watchlist" in c.post("/watchlist/add", data={"symbol": "dn", "next": "/scan"}).text
    assert "Avoid" in c.get("/watchlist").text
    assert "Settings saved" in c.post("/settings", data={k: v for k, v in db.DEFAULT_SETTINGS.items()} | {"stop_loss_pct": "8"}).text
    assert conn.execute("SELECT value FROM settings WHERE key='stop_loss_pct'").fetchone()[0] == "8"
    assert "not a number" in c.post("/settings", data={k: v for k, v in db.DEFAULT_SETTINGS.items()} | {"stop_loss_pct": "x"}).text  # re-rendered with the error
    assert c.get("/report/today", follow_redirects=False).status_code == 307


# --- strategy page -------------------------------------------------------------


def test_score_rules_cover_the_score():
    ids = {r[0] for r in analysis.SCORE_RULES}
    assert sum(r[4] for r in analysis.SCORE_RULES) == 100
    aspi = pd.Series([1000.0] * 250, index=frame(uptrend()).index)
    for closes in (uptrend(), list(reversed(uptrend())), uptrend(60)):
        _, comps = analysis.score(analysis.compute_features("X", "X", frame(closes), aspi))
        assert {c.rule for c in comps} - {"history"} <= ids


def test_strategy_page(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cse_analyzer import strategy

    monkeypatch.setenv("CSE_DB_PATH", str(tmp_path / "t.db"))
    from cse_analyzer.web import app

    c = TestClient(app)
    assert "No analysis yet" in c.get("/strategy").text
    days = frame(uptrend()).index
    save_trades(conn, [TradeRow(date.fromisoformat(days[100]), "UP.N0000", "BUY", 100, uptrend()[100])], "form")
    conn.execute("INSERT INTO watchlist (symbol) VALUES ('DN.N0000')")
    conn.commit()
    analysis.run_analysis(conn)

    data = strategy.build(conn, SETTINGS)
    assert data["tagged"] and data["n_scored"] == 3
    assert sum(data["hcount"].values()) == 1
    assert data["ecount"]["downtrend"]["watch"] == 1
    assert [m["sig"]["symbol"] for m in data["matrix"]] == ["UP.N0000", "DN.N0000"]
    assert sum(data["matrix"][0]["groups"].values()) == data["matrix"][0]["sig"]["score"]
    assert data["ladder"][0]["symbol"] == "UP.N0000" and data["ladder"][0]["stage"] in ("break-even", "trailing")
    assert data["whatif"]["holdings"][0][1]["Hold"] + data["whatif"]["holdings"][0][1]["Add"] + \
        data["whatif"]["holdings"][0][1]["Trim"] == 1
    assert sum(b["n"] for b in data["dist"]) == 3

    page = c.get("/strategy").text
    for text in ("The strategy in one minute", "Your stocks, rule by rule", "Exits for your holdings",
                 "How a verdict is decided", "How the score is built", "What if you changed a setting?"):
        assert text in page
