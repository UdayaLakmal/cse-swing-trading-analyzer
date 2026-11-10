import io
from datetime import date

import pytest
from openpyxl import Workbook, load_workbook

from cse_analyzer import db
from cse_analyzer.portfolio import build_portfolio
from cse_analyzer.trades import (
    SymbolResolver,
    TradeRow,
    parse_paste,
    parse_xlsx,
    save_trades,
    xlsx_template,
)

SYMBOLS = ["JKH.N0000", "COMB.N0000", "COMB.X0000", "HNB.N0000", "HNB.X0000", "CALI.U0000"]
TODAY = date(2026, 11, 10)


@pytest.fixture
def resolver():
    return SymbolResolver(SYMBOLS)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    for i, s in enumerate(SYMBOLS, 1):
        c.execute("INSERT INTO stocks VALUES (?, ?, ?, NULL, '')", (i, s, s.split(".")[0] + " PLC"))
    c.commit()
    return c


def test_resolver(resolver):
    assert resolver.resolve("jkh") == "JKH.N0000"
    assert resolver.resolve("COMB.X") == "COMB.X0000"
    assert resolver.resolve("HNB.X0000") == "HNB.X0000"
    assert resolver.resolve("CALI") == "CALI.U0000"
    assert resolver.resolve("NOPE") is None


def test_parse_paste_formats(resolver):
    text = """
    # comment
    JKH 500 @ 19.50
    SELL COMB 100 @ 120 2026-11-01
    2026-11-03 B HNB 1,000 @ 310 fees 3500
    sold jkh 200 at 21 05/11/2026
    JKH 10 x 20
    XYZ 10 @ 5
    JKH 1.5 @ 10
    garbage
    """
    r = parse_paste(text, resolver, TODAY)
    assert [(t.day, t.symbol, t.side, t.quantity, t.price, t.fees) for t in r.rows] == [
        (TODAY, "JKH.N0000", "BUY", 500, 19.5, None),
        (date(2026, 11, 1), "COMB.N0000", "SELL", 100, 120.0, None),
        (date(2026, 11, 3), "HNB.N0000", "BUY", 1000, 310.0, 3500.0),
        (date(2026, 11, 5), "JKH.N0000", "SELL", 200, 21.0, None),
        (TODAY, "JKH.N0000", "BUY", 10, 20.0, None),
    ]
    assert len(r.errors) == 3
    assert "unknown symbol" in r.errors[0]


def test_xlsx_roundtrip(resolver):
    wb = load_workbook(io.BytesIO(xlsx_template()))
    ws = wb.active
    ws.append(["2026-11-04", "comb", "sell", 50, 125, 70, None])
    ws.append([None] * 7)
    ws.append(["2026-11-04", "COMB", "HOLD", 50, 125, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    r = parse_xlsx(buf.getvalue(), resolver)
    assert [(t.day, t.symbol, t.side, t.quantity, t.price, t.fees) for t in r.rows] == [
        (date(2026, 11, 3), "JKH.N0000", "BUY", 500, 19.5, None),
        (date(2026, 11, 4), "COMB.N0000", "SELL", 50, 125.0, 70.0),
    ]
    assert len(r.errors) == 1 and "Side" in r.errors[0]


def test_xlsx_missing_columns(resolver):
    wb = Workbook()
    wb.active.append(["Date", "Symbol"])
    buf = io.BytesIO()
    wb.save(buf)
    r = parse_xlsx(buf.getvalue(), resolver)
    assert r.errors and "missing columns" in r.errors[0]


def test_save_skips_duplicates(conn):
    row = TradeRow(TODAY, "JKH.N0000", "BUY", 100, 20.0)
    assert save_trades(conn, [row, row], "paste") == (1, 1)
    assert save_trades(conn, [row], "xlsx") == (0, 1)


def test_portfolio_average_cost_and_fees(conn):
    conn.execute("UPDATE settings SET value = '1' WHERE key = 'fee_rate_pct'")
    save_trades(conn, [
        TradeRow(date(2026, 11, 1), "JKH.N0000", "BUY", 100, 20.0),           # fee 20
        TradeRow(date(2026, 11, 2), "JKH.N0000", "BUY", 100, 22.0, fees=0),   # fee 0
        TradeRow(date(2026, 11, 3), "JKH.N0000", "SELL", 50, 25.0),           # fee 12.5
    ], "form")
    conn.execute("INSERT INTO prices_daily VALUES (1, '2026-11-09', 0, 0, 30.0, 0)")
    conn.commit()
    pf = build_portfolio(conn)
    (h,) = pf.holdings
    # cost before sell: 2000 + 20 + 2200 = 4220 for 200 -> avg 21.10
    assert h.quantity == 150
    assert h.cost == pytest.approx(4220 * 150 / 200)
    assert h.entry_price == pytest.approx(21.0)
    assert pf.realised["JKH.N0000"] == pytest.approx(50 * 25 - 12.5 - 21.1 * 50)
    assert h.unrealised == pytest.approx(150 * 30 * 0.99 - h.cost)
    assert h.break_even == pytest.approx(21.1 / 0.99)
    assert h.effective_stop == pytest.approx(21.0 * 0.9)
    assert not h.below_stop
    assert h.weight_pct == pytest.approx(100)
    assert pf.fees_paid == pytest.approx(32.5)


def test_portfolio_closed_position_and_oversell(conn):
    save_trades(conn, [
        TradeRow(date(2026, 11, 1), "HNB.N0000", "BUY", 10, 300.0, fees=0),
        TradeRow(date(2026, 11, 2), "HNB.N0000", "SELL", 15, 310.0, fees=0),
    ], "form")
    pf = build_portfolio(conn)
    assert pf.holdings == []
    assert pf.realised["HNB.N0000"] == pytest.approx(100.0)
    assert any("only 10 held" in w for w in pf.warnings)


def test_per_stock_stop_and_trailing(conn):
    save_trades(conn, [TradeRow(date(2026, 11, 1), "JKH.N0000", "BUY", 100, 20.0)], "form")
    conn.execute("INSERT INTO prices_daily VALUES (1, '2026-11-09', 0, 0, 17.5, 0)")
    conn.execute("INSERT INTO position_settings (symbol, stop_loss_pct) VALUES ('JKH.N0000', 5)")
    conn.commit()
    (h,) = build_portfolio(conn).holdings
    assert h.effective_stop == pytest.approx(19.0)
    assert h.below_stop
    conn.execute("UPDATE position_settings SET stop_price = 19.5, stop_entry_day = '2026-11-01'")
    conn.commit()
    assert build_portfolio(conn).holdings[0].effective_stop == pytest.approx(19.5)
    # A stop stored for an earlier position of the same stock does not apply.
    conn.execute("UPDATE position_settings SET stop_entry_day = '2026-01-01'")
    conn.commit()
    assert build_portfolio(conn).holdings[0].effective_stop == pytest.approx(19.0)


def test_web_flows(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CSE_DB_PATH", str(tmp_path / "t.db"))
    from cse_analyzer.web import app

    c = TestClient(app)
    assert "No holdings yet" in c.get("/holdings").text

    r = c.post("/trades/add", data={"day": "2026-11-01", "symbol": "jkh", "side": "BUY", "quantity": 100, "price": 20})
    assert "Saved BUY 100 JKH.N0000" in r.text

    r = c.post("/trades/paste", data={"text": "JKH 100 @ 20 2026-11-01\nCOMB 10 @ 100\nBAD 1 @ 1"})
    assert "already saved" in r.text and "Save 1 new trade" in r.text and "unknown symbol" in r.text
    payload = r.text.split('name="payload" value="')[1].split('"')[0].replace("&#34;", '"')
    r = c.post("/trades/confirm", data={"payload": payload, "source": "paste"})
    assert "Saved 1 trades" in r.text

    r = c.post("/trades/upload", files={"file": ("t.xlsx", xlsx_template())})
    assert "Save 1 new trade" in r.text

    page = c.get("/holdings").text
    assert "JKH.N0000" in page and "COMB.N0000" in page

    assert "Stop-loss for JKH.N0000 set to 7%" in c.post("/holdings/JKH.N0000/stop", data={"stop_loss_pct": "7"}).text
    assert c.post("/holdings/JKH.N0000/stop", data={"stop_loss_pct": "150"}).status_code == 400

    assert "HNB.N0000 is on your watchlist" in c.post("/watchlist/add", data={"symbol": "hnb", "target_entry": "290"}).text
    assert "HNB.N0000 removed from the watchlist" in c.post("/watchlist/HNB.N0000/delete").text

    tid = conn.execute("SELECT id FROM trades WHERE symbol = 'COMB.N0000'").fetchone()[0]
    assert "Trade deleted" in c.post(f"/trades/{tid}/delete").text
    assert c.get("/trades/template.xlsx").headers["content-type"].startswith("application/vnd.openxml")


# --- broker statement (made-up numbers, same layout as the broker export) ---

STATEMENT_HEADER = [
    "Security", "Quantity", "Cleared Balance", "Available Balance", "Unsettled Buy", "Unsettled Sell",
    "Holding % (Quantity)", "Avg Price", "B.E.S Price", "Total Cost", "Traded Price", "Market Value",
]


def statement_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Portfolio"
    ws.append(["Portfolio (ACCOUNT) - EQUITY"])
    ws.append([])
    ws.append(STATEMENT_HEADER)
    for sym, qty, avg in rows:
        ws.append([sym, qty, qty, qty, 0, 0, 0, avg, 0, qty * avg, 0, 0])
    ws.append(["Total", None, None, None, None, None, None, None, None, 0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_statement_becomes_opening_buys(resolver):
    data = statement_xlsx([("JKH.N0000", 1000, 20.224), ("HNB.N0000", 0, 300), ("NOPE.N0000", 5, 1)])
    r = parse_xlsx(data, resolver, fee_rate=0.0112, statement_day=TODAY)
    assert r.kind == "statement"
    (t,) = r.rows
    assert (t.day, t.symbol, t.side, t.quantity) == (TODAY, "JKH.N0000", "BUY", 1000)
    assert t.price == pytest.approx(20.0, abs=1e-4)
    assert t.quantity * t.price + t.fees == pytest.approx(20224.0, abs=0.01)
    assert len(r.errors) == 1 and "NOPE" in r.errors[0]


def test_statement_preview_never_doubles_holdings(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CSE_DB_PATH", str(tmp_path / "t.db"))
    from cse_analyzer.web import app

    c = TestClient(app)
    save_trades(conn, [
        TradeRow(date(2026, 10, 1), "JKH.N0000", "BUY", 1000, 20.0),
        TradeRow(date(2026, 10, 1), "COMB.N0000", "BUY", 50, 100.0),
        TradeRow(date(2026, 10, 1), "CALI.U0000", "BUY", 10, 10.0),
    ], "form")
    data = statement_xlsx([("JKH.N0000", 1000, 20.2), ("COMB.N0000", 80, 101), ("HNB.N0000", 10, 303)])
    r = c.post("/trades/upload", files={"file": ("p.xlsx", data)})
    t = r.text
    assert "broker holdings statement" in t
    assert "already held" in t and "held 50 here vs 80 on statement" in t
    assert "CALI.U0000: the app holds 10" in t
    assert "Save 1 new trade" in t
    payload = t.split('name="payload" value="')[1].split('"')[0].replace("&#34;", '"')
    assert "HNB.N0000" in payload and "JKH" not in payload
    assert "Saved 1 trades" in c.post("/trades/confirm", data={"payload": payload, "source": "statement"}).text
    assert conn.execute("SELECT source FROM trades WHERE symbol = 'HNB.N0000'").fetchone()[0] == "statement"
