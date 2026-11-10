"""Web app: Today report, holdings, watchlist, market scan, stock pages, trades, settings."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from . import analysis, charts, collector, db
from .cli import status
from .config import load_settings
from .portfolio import build_portfolio
from .trades import SymbolResolver, TradeRow, parse_paste, parse_xlsx, save_trades, xlsx_template

app = FastAPI(title="CSE Swing-Trading Analyzer")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

VERDICT_ORDER = {"Sell": 0, "Trim": 1, "Add": 2, "Enter": 3, "Hold": 4, "Wait": 5, "Avoid": 6, "No data": 7}


def _missing(v) -> bool:
    return v is None or isinstance(v, Undefined)


def money(v: float | None, digits: int = 2) -> str:
    return "–" if _missing(v) else f"{v:,.{digits}f}"


def signed(v: float | None, digits: int = 2) -> str:
    return "–" if _missing(v) else f"{v:+,.{digits}f}"


def compact(v: float | None) -> str:
    if _missing(v):
        return "–"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:,.1f}{suffix}"
    return f"{v:,.0f}"


templates.env.filters.update(money=money, signed=signed, compact=compact)
templates.env.globals["vclass"] = lambda v: (v or "").lower().replace(" ", "-")


def get_conn() -> Iterator:
    conn = db.connect(load_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


def settings_of(conn) -> dict[str, float]:
    return {k: float(v) for k, v in conn.execute("SELECT key, value FROM settings")}


def render(request: Request, name: str, conn, **ctx) -> HTMLResponse:
    latest = collector.latest_price_day(conn)
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.update(
        request=request,
        stale=collector.is_stale(conn),
        latest_day=latest.isoformat() if latest else None,
        symbols=[r[0] for r in conn.execute("SELECT symbol FROM stocks ORDER BY symbol")],
        today=date.today().isoformat(),
    )
    return templates.TemplateResponse(request, name, ctx)


def back(path: str, msg: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(path if not msg else f"{path}{sep}msg={quote(msg)}", status_code=303)


def closes(conn, symbols: list[str], n: int) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for sym in symbols:
        rows = conn.execute(
            """SELECT p.close FROM prices_daily p JOIN stocks s USING (stock_id)
               WHERE s.symbol = ? ORDER BY p.day DESC LIMIT ?""", (sym, n)).fetchall()
        out[sym] = [r[0] for r in reversed(rows)]
    return out


# --- Today -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def today_page(request: Request, conn=Depends(get_conn)):
    day, sigs = analysis.load_signals(conn)
    pf = build_portfolio(conn)
    regime = analysis.market_regime(conn)
    s = settings_of(conn)
    by_sym = {x["symbol"]: x for x in sigs}

    rows, day_change = [], 0.0
    for h in pf.holdings:
        sig = by_sym.get(h.symbol, {})
        feats = sig.get("features") or {}
        prev = feats.get("prev_close")
        chg = h.quantity * (h.last_close - prev) if prev and h.last_close else None
        day_change += chg or 0
        rows.append({"h": h, "sig": sig, "day_change": chg})
    rows.sort(key=lambda r: (VERDICT_ORDER.get(r["sig"].get("verdict"), 9), -(r["h"].market_value or 0)))

    watch = sorted((x for x in sigs if x["list"] == "watch"),
                   key=lambda x: (VERDICT_ORDER.get(x["verdict"], 9), -x["score"]))
    actions = sorted(
        [x for x in sigs if (x["list"] == "holding" and x["verdict"] in ("Sell", "Trim", "Add"))
         or (x["list"] == "watch" and x["verdict"] == "Enter")],
        key=lambda x: (VERDICT_ORDER[x["verdict"]], -x["score"]),
    )
    ideas = sorted((x for x in sigs if x["list"] == "scan" and x["verdict"] == "Enter"), key=lambda x: -x["score"])[:6]
    sparks = {k: charts.sparkline(v) for k, v in closes(conn, [r["h"].symbol for r in rows] + [w["symbol"] for w in watch], 40).items()}
    prev_mv = pf.market_value - day_change
    return render(
        request, "today.html", conn, active="today", day=day, pf=pf, rows=rows, watch=watch, actions=actions,
        ideas=ideas, regime=regime, sparks=sparks, day_change=day_change,
        day_change_pct=(100 * day_change / prev_mv) if prev_mv else None, settings=s,
        analysis_behind=bool(day and (latest := collector.latest_price_day(conn)) and day < latest.isoformat()),
        scan_counts={v: sum(1 for x in sigs if x["list"] == "scan" and x["verdict"] == v) for v in ("Enter", "Wait", "Avoid")},
    )


@app.get("/report/today")
def report_today():
    return RedirectResponse("/", status_code=307)


@app.post("/analysis/run")
def run_analysis_now(conn=Depends(get_conn)):
    r = analysis.run_analysis(conn)
    return back("/", f"Analysis refreshed for {r['day']}: {r['analysed']} stocks scored")


# --- Stock page ------------------------------------------------------------


@app.get("/go")
def go(q: str = "", conn=Depends(get_conn)):
    sym = SymbolResolver.from_db(conn).resolve(q)
    if not sym:
        return back("/", f"No listed stock matches '{q}'")
    return RedirectResponse(f"/stock/{sym}", status_code=303)


@app.get("/stock/{symbol}", response_class=HTMLResponse)
def stock_page(symbol: str, request: Request, conn=Depends(get_conn)):
    stock = conn.execute("SELECT * FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
    if not stock:
        raise HTTPException(404, "Unknown symbol")
    prices = conn.execute(
        "SELECT day, high, low, close, volume FROM prices_daily WHERE stock_id = ? ORDER BY day", (stock["stock_id"],)
    ).fetchall()
    import pandas as pd

    from . import indicators as ind

    close = pd.Series([p["close"] for p in prices], dtype=float)
    days = [p["day"] for p in prices]
    ma50 = [None if pd.isna(v) else float(v) for v in ind.sma(close, 50)]
    ma200 = [None if pd.isna(v) else float(v) for v in ind.sma(close, 200)]

    _, sigs = analysis.load_signals(conn)
    sig = next((x for x in sigs if x["symbol"] == symbol), None)
    holding = next((h for h in build_portfolio(conn).holdings if h.symbol == symbol), None)
    levels = {}
    if sig and sig.get("stop"):
        levels["Stop"] = (sig["stop"], "var(--sell)")
    if sig and sig.get("target"):
        levels["Target"] = (sig["target"], "var(--buy)")
    if holding:
        levels["Entry"] = (holding.entry_price, "var(--muted)")
    chart = charts.price_chart(days, list(close), {"50-day": (ma50, "var(--c1)"), "200-day": (ma200, "var(--c2)")}, levels)

    groups: dict[str, list] = {}
    for comp in (sig or {}).get("components", []):
        groups.setdefault(comp["group"], []).append(comp)
    base = stock["symbol"].split(".")[0]
    news = conn.execute(
        """SELECT * FROM announcements WHERE symbol IN (?, ?) OR company = ?
           ORDER BY fetched_at DESC, id DESC LIMIT 8""", (base, symbol, stock["name"])).fetchall()
    watched = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    trades = conn.execute("SELECT * FROM trades WHERE symbol = ? ORDER BY day DESC, id DESC", (symbol,)).fetchall()
    return render(request, "stock.html", conn, active="", stock=stock, sig=sig, f=(sig or {}).get("features") or {},
                  chart=chart, groups=groups, holding=holding, news=news, watched=watched, trades=trades,
                  settings=settings_of(conn))


# --- Market scan -----------------------------------------------------------


@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request, v: str = "Enter", conn=Depends(get_conn)):
    day, sigs = analysis.load_signals(conn)
    rows = sorted((x for x in sigs if v == "All" or x["verdict"] == v), key=lambda x: -x["score"])
    counts = {k: sum(1 for x in sigs if x["verdict"] == k) for k in ("Enter", "Wait", "Avoid", "Hold", "Trim", "Sell", "Add")}
    watched = {r[0] for r in conn.execute("SELECT symbol FROM watchlist")}
    return render(request, "scan.html", conn, active="scan", day=day, rows=rows, v=v, counts=counts,
                  total=len(sigs), watched=watched)


# --- Holdings --------------------------------------------------------------


@app.get("/holdings", response_class=HTMLResponse)
def holdings_page(request: Request, conn=Depends(get_conn)):
    _, sigs = analysis.load_signals(conn)
    by_sym = {x["symbol"]: x for x in sigs}
    return render(request, "holdings.html", conn, active="holdings", pf=build_portfolio(conn), sigs=by_sym)


@app.post("/holdings/{symbol}/stop")
def set_stop(symbol: str, stop_loss_pct: str = Form(""), conn=Depends(get_conn)):
    value = float(stop_loss_pct) if stop_loss_pct.strip() else None
    if value is not None and not 0 < value < 100:
        raise HTTPException(400, "stop-loss % must be between 0 and 100")
    # A new stop % restarts the trailing stop from the new initial level.
    conn.execute(
        """INSERT INTO position_settings (symbol, stop_loss_pct) VALUES (?, ?)
           ON CONFLICT(symbol) DO UPDATE SET stop_loss_pct = excluded.stop_loss_pct, stop_price = NULL""",
        (symbol, value),
    )
    conn.commit()
    analysis.run_analysis(conn)
    return back("/holdings", f"Stop-loss for {symbol} set to {value:g}%" if value else f"{symbol} uses the default stop again")


# --- Trades ----------------------------------------------------------------


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM trades ORDER BY day DESC, id DESC").fetchall()
    return render(request, "trades.html", conn, active="trades", trades=rows)


@app.post("/trades/add")
def add_trade(
    day: str = Form(...),
    symbol: str = Form(...),
    side: str = Form(...),
    quantity: int = Form(...),
    price: float = Form(...),
    fees: str = Form(""),
    note: str = Form(""),
    conn=Depends(get_conn),
):
    sym = SymbolResolver.from_db(conn).resolve(symbol)
    if not sym:
        return back("/trades", f"Unknown symbol {symbol}")
    if quantity <= 0 or price <= 0 or side not in ("BUY", "SELL"):
        return back("/trades", "Quantity and price must be above 0")
    row = TradeRow(date.fromisoformat(day), sym, side, quantity, price,
                   float(fees) if fees.strip() else None, note.strip() or None)
    added, _ = save_trades(conn, [row], "form")
    if added:
        analysis.run_analysis(conn)
    return back("/trades", f"Saved {side} {quantity:,} {sym}" if added else "That exact trade is already saved")


@app.post("/trades/paste", response_class=HTMLResponse)
def paste_preview(request: Request, text: str = Form(...), conn=Depends(get_conn)):
    result = parse_paste(text, SymbolResolver.from_db(conn), date.today())
    return _preview(request, conn, result, "paste")


@app.post("/trades/upload", response_class=HTMLResponse)
async def upload_preview(request: Request, file: UploadFile = File(...), conn=Depends(get_conn)):
    fee_rate = settings_of(conn).get("fee_rate_pct", 1.12) / 100
    result = parse_xlsx(
        await file.read(), SymbolResolver.from_db(conn),
        fee_rate=fee_rate, statement_day=collector.now_colombo().date(),
    )
    return _preview(request, conn, result, "statement" if result.kind == "statement" else "xlsx")


def _preview(request, conn, result, source):
    existing = {
        (r["day"], r["symbol"], r["side"], r["quantity"], r["price"])
        for r in conn.execute("SELECT day, symbol, side, quantity, price FROM trades")
    }
    held = {h.symbol: h.quantity for h in build_portfolio(conn).holdings}
    notes = []
    rows = []
    for r in result.rows:
        row = {**r.__dict__, "day": r.day.isoformat(), "include": True, "status": "new"}
        if (row["day"], r.symbol, r.side, r.quantity, r.price) in existing:
            row.update(include=False, status="already saved")
        elif result.kind == "statement" and r.symbol in held:
            # A statement is a snapshot, not a trade: never add on top of a
            # position the app already tracks.
            have = held[r.symbol]
            row.update(include=False, status="already held" if have == r.quantity
                       else f"held {have:,} here vs {r.quantity:,} on statement")
            if have != r.quantity:
                notes.append(f"{r.symbol}: the app has {have:,} shares, the statement {r.quantity:,}. "
                             "Add the buys or sells that explain the difference on the Trades page.")
        rows.append(row)
    if result.kind == "statement":
        on_statement = {r.symbol for r in result.rows}
        for sym, q in sorted(held.items()):
            if sym not in on_statement:
                notes.append(f"{sym}: the app holds {q:,} but it is not on the statement. Add the sell if you sold it.")

    keep = ("day", "symbol", "side", "quantity", "price", "fees", "note")
    payload = json.dumps([{k: r[k] for k in keep} for r in rows if r["include"]])
    return render(request, "preview.html", conn, active="trades", rows=rows, errors=result.errors, notes=notes,
                  payload=payload, source=source, kind=result.kind)


@app.post("/trades/confirm")
def confirm(payload: str = Form(...), source: str = Form(...), conn=Depends(get_conn)):
    rows = [
        TradeRow(date.fromisoformat(r["day"]), r["symbol"], r["side"], int(r["quantity"]),
                 float(r["price"]), r.get("fees"), r.get("note"))
        for r in json.loads(payload)
    ]
    added, skipped = save_trades(conn, rows, source if source in ("paste", "xlsx", "statement") else "form")
    if added:
        analysis.run_analysis(conn)
    return back("/trades", f"Saved {added} trades" + (f", skipped {skipped} duplicates" if skipped else ""))


@app.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int, conn=Depends(get_conn)):
    conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    conn.commit()
    analysis.run_analysis(conn)
    return back("/trades", "Trade deleted")


@app.get("/trades/template.xlsx")
def template():
    return Response(
        xlsx_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="cse-trades-template.xlsx"'},
    )


# --- Watchlist -------------------------------------------------------------


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, conn=Depends(get_conn)):
    _, sigs = analysis.load_signals(conn)
    by_sym = {x["symbol"]: x for x in sigs}
    rows = conn.execute(
        """SELECT w.*, s.name,
                  (SELECT p.close FROM prices_daily p WHERE p.stock_id = s.stock_id ORDER BY p.day DESC LIMIT 1) AS last_close
           FROM watchlist w LEFT JOIN stocks s USING (symbol) ORDER BY w.symbol"""
    ).fetchall()
    items = sorted(({"w": r, "sig": by_sym.get(r["symbol"], {})} for r in rows),
                   key=lambda x: (VERDICT_ORDER.get(x["sig"].get("verdict"), 9), x["w"]["symbol"]))
    sparks = {k: charts.sparkline(v) for k, v in closes(conn, [r["symbol"] for r in rows], 40).items()}
    return render(request, "watchlist.html", conn, active="watchlist", items=items, sparks=sparks)


@app.post("/watchlist/add")
def watchlist_add(symbol: str = Form(...), target_entry: str = Form(""), note: str = Form(""),
                  next: str = Form("/watchlist"), conn=Depends(get_conn)):
    sym = SymbolResolver.from_db(conn).resolve(symbol)
    dest = next if next.startswith("/") and not next.startswith("//") else "/watchlist"
    if not sym:
        return back(dest, f"Unknown symbol {symbol}")
    conn.execute(
        """INSERT INTO watchlist (symbol, target_entry, note) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET target_entry = excluded.target_entry, note = excluded.note""",
        (sym, float(target_entry) if target_entry.strip() else None, note.strip() or None),
    )
    conn.commit()
    analysis.run_analysis(conn)
    return back(dest, f"{sym} is on your watchlist")


@app.post("/watchlist/{symbol}/delete")
def watchlist_delete(symbol: str, next: str = Form("/watchlist"), conn=Depends(get_conn)):
    conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
    conn.commit()
    analysis.run_analysis(conn)
    dest = next if next.startswith("/") and not next.startswith("//") else "/watchlist"
    return back(dest, f"{symbol} removed from the watchlist")


# --- Settings --------------------------------------------------------------

SETTINGS_FORM = [
    ("Risk", [
        ("stop_loss_pct", "Default stop-loss", "%", "Initial stop below your entry price. Per-stock overrides are on the Holdings page."),
        ("risk_per_trade_pct", "Risk per trade", "% of capital", "Used to suggest a position size for Enter signals."),
        ("max_weight_pct", "Max weight per stock", "%", "Holdings above this get a Trim verdict."),
    ]),
    ("Trailing stop", [
        ("breakeven_at_r", "Move stop to break-even at", "R", "1R is the size of your initial risk (e.g. +10%)."),
        ("chandelier_atr_mult", "Chandelier trail", "x ATR", "After break-even, the stop trails the highest close since entry by this many ATRs."),
        ("partial_take_at_r", "Take partial profit at", "R", "Trim about a third once the gain reaches this."),
    ]),
    ("Entry signals", [
        ("enter_rsi_low", "RSI entry zone from", "", "Enter needs RSI between these two after a pullback."),
        ("enter_rsi_high", "RSI entry zone to", "", ""),
        ("min_turnover_lkr", "Minimum daily turnover", "LKR", "Stocks trading less than this on average are marked Avoid."),
    ]),
    ("Costs", [
        ("fee_rate_pct", "Brokerage, levies and tax", "% per side", "Used when a trade has no fees entered, and for net P/L."),
    ]),
]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn=Depends(get_conn)):
    return render(request, "settings.html", conn, active="settings", form=SETTINGS_FORM,
                  values=dict(conn.execute("SELECT key, value FROM settings").fetchall()))


@app.post("/settings")
async def settings_save(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    keys = [k for _, fields in SETTINGS_FORM for k, *_ in fields]
    for k in keys:
        raw = str(form.get(k, "")).strip()
        try:
            v = float(raw)
        except ValueError:
            return back("/settings", f"'{raw}' is not a number")
        if v < 0:
            return back("/settings", "Values cannot be negative")
        conn.execute("UPDATE settings SET value = ? WHERE key = ?", (f"{v:g}", k))
    conn.commit()
    analysis.run_analysis(conn)
    return back("/settings", "Settings saved and the analysis refreshed")


# --- API -------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def api_status(conn=Depends(get_conn)) -> dict:
    return status(conn)
