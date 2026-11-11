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

from . import analysis, charts, collector, db, fundamentals, news, settings_store, strategy
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
    mine = [h.symbol for h in pf.holdings] + [w["symbol"] for w in watch]
    evs = fundamentals.events(conn, mine, days=10)
    coming = sorted(((s_, e) for s_, lst in evs.items() for e in lst if e.get("upcoming")), key=lambda x: x[1]["day"])
    lately = sorted(((s_, e) for s_, lst in evs.items() for e in lst if not e.get("upcoming")),
                    key=lambda x: x[1]["day"], reverse=True)[:8]
    headlines = news.latest(conn, mine, days=7, limit=8)
    sparks = {k: charts.sparkline(v) for k, v in closes(conn, [r["h"].symbol for r in rows] + [w["symbol"] for w in watch], 40).items()}
    prev_mv = pf.market_value - day_change
    return render(
        request, "today.html", conn, active="today", day=day, pf=pf, rows=rows, watch=watch, actions=actions,
        ideas=ideas, regime=regime, sparks=sparks, day_change=day_change, coming=coming, lately=lately,
        headlines=headlines,
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
    closes_now = latest_closes(conn, [symbol])
    val = fundamentals.valuations(conn, closes_now).get(symbol)
    evs = fundamentals.events(conn, [symbol], days=400)[symbol][:12]
    headlines = news.latest(conn, [symbol], days=60, limit=10)
    watched = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    trades = conn.execute("SELECT * FROM trades WHERE symbol = ? ORDER BY day DESC, id DESC", (symbol,)).fetchall()
    return render(request, "stock.html", conn, active="", stock=stock, sig=sig, f=(sig or {}).get("features") or {},
                  chart=chart, groups=groups, holding=holding, events=evs, headlines=headlines, val=val,
                  watched=watched, trades=trades,
                  settings=settings_of(conn))


# --- Fundamentals & news ---------------------------------------------------------


def latest_closes(conn, symbols: list[str]) -> dict[str, float]:
    out = {}
    for sym in symbols:
        r = conn.execute("""SELECT p.close FROM prices_daily p JOIN stocks s USING (stock_id)
                            WHERE s.symbol = ? ORDER BY p.day DESC LIMIT 1""", (sym,)).fetchone()
        if r and r[0]:
            out[sym] = r[0]
    return out


@app.get("/fundamentals", response_class=HTMLResponse)
def fundamentals_page(request: Request, conn=Depends(get_conn)):
    tracked = fundamentals.tracked_symbols(conn)
    extra = [r[0] for r in conn.execute(
        "SELECT symbol FROM fundamentals WHERE eps_ttm IS NOT NULL OR nav_manual IS NOT NULL")]
    syms = sorted(set(tracked) | {s for s in extra if conn.execute("SELECT 1 FROM stocks WHERE symbol = ?", (s,)).fetchone()})
    vals = fundamentals.valuations(conn, latest_closes(conn, syms))
    held = {h.symbol for h in build_portfolio(conn).holdings}
    watch = {r[0] for r in conn.execute("SELECT symbol FROM watchlist")}
    names = dict(conn.execute("SELECT symbol, name FROM stocks"))
    evs = fundamentals.events(conn, syms, days=60)
    run = conn.execute("SELECT * FROM runs WHERE kind = 'weekly' ORDER BY id DESC LIMIT 1").fetchone()
    rows = [{"symbol": s, "name": names.get(s, s), "v": vals.get(s),
             "role": "holding" if s in held else ("watchlist" if s in watch else ("idea" if s in tracked else "kept")),
             "events": evs.get(s, [])} for s in syms]
    order = {"holding": 0, "watchlist": 1, "idea": 2, "kept": 3}
    rows.sort(key=lambda r: (order[r["role"]], r["symbol"]))
    upcoming = sorted(((r["symbol"], e) for r in rows for e in r["events"] if e.get("upcoming")), key=lambda x: x[1]["day"])
    recent = sorted(((r["symbol"], e) for r in rows for e in r["events"] if not e.get("upcoming")),
                    key=lambda x: x[1]["day"], reverse=True)[:25]
    return render(request, "fundamentals.html", conn, active="fundamentals", rows=rows, market=fundamentals.market(conn),
                  upcoming=upcoming, recent=recent, news=news.latest(conn, syms, days=21, limit=40),
                  market_news=news.latest(conn, [news.MARKET], days=7, limit=8), run=dict(run) if run else None, names=names)


def _num_or_none(text: str, *, positive: bool = False) -> float | None:
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    v = float(text)  # ValueError handled by the caller
    if positive and v <= 0:
        raise ValueError
    return v


@app.post("/fundamentals/refresh")
def fundamentals_refresh(conn=Depends(get_conn)):
    """Same as the weekly job, run now: new announcements, dividends and
    report PDFs for tracked stocks, then news."""
    s = load_settings()
    from .cse_client import CseClient

    try:
        with CseClient(s.cse_base_url, s.request_delay_s, s.http_timeout_s) as client:
            st = fundamentals.collect_weekly(conn, client)
        ns = news.collect_news(conn, fundamentals.tracked_symbols(conn), delay_s=0.5)
    except Exception as e:  # noqa: BLE001 - show the reason instead of a 500
        return back("/fundamentals", f"Refresh failed: {e}")
    analysis.run_analysis(conn)
    return back("/fundamentals", f"Refreshed {st['stocks']} stocks: {st['nav_new']} reports read, "
                                 f"{st['dividends']} new dividends, {ns['saved']} new headlines.")


@app.post("/fundamentals/{symbol}")
def fundamentals_save(symbol: str, eps: str = Form(""), nav: str = Form(""), note: str = Form(""),
                      next: str = Form("/fundamentals"), conn=Depends(get_conn)):
    if not conn.execute("SELECT 1 FROM stocks WHERE symbol = ?", (symbol,)).fetchone():
        raise HTTPException(404, "Unknown symbol")
    nxt = next if next.startswith("/") and not next.startswith("//") else "/fundamentals"
    try:
        e = _num_or_none(eps)
    except ValueError:
        return back(nxt, f"{symbol}: EPS must be a number such as 2.35 or -0.40")
    try:
        n = _num_or_none(nav, positive=True)
    except ValueError:
        return back(nxt, f"{symbol}: net assets per share must be a number above zero")
    fundamentals.save_manual(conn, symbol, eps=e, nav=n, note=note.strip()[:200] or None)
    analysis.run_analysis(conn)
    return back(nxt, f"{symbol.split('.')[0]}: saved. The valuation and analysis are refreshed.")


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


# --- Strategy --------------------------------------------------------------


@app.get("/strategy", response_class=HTMLResponse)
def strategy_page(request: Request, conn=Depends(get_conn)):
    s = settings_of(conn)
    return render(request, "strategy.html", conn, active="strategy", s=s, **strategy.build(conn, s))


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


LABELS = {k: label for _, fields in SETTINGS_FORM for k, label, *_ in fields}


def _settings_page(request, conn, values=None, errors=None):
    return render(request, "settings.html", conn, active="settings", form=SETTINGS_FORM,
                  values=values or settings_store.current(conn), errors=errors or {},
                  defaults=db.DEFAULT_SETTINGS, limits=settings_store.LIMITS, labels=LABELS,
                  history=settings_store.history(conn), presets=settings_store.presets(conn),
                  nice=settings_store.nice, cur=settings_store.current(conn))


def _applied(conn, changed: list[str], what: str) -> RedirectResponse:
    if not changed:
        return back("/settings", f"{what}: nothing changed")
    analysis.run_analysis(conn)
    names = ", ".join(LABELS[k].lower() for k in changed)
    extra = " Trailing stops were worked out again for the new values." if settings_store.STOP_KEYS & set(changed) else ""
    return back("/settings", f"{what}: changed {names}. The analysis is refreshed.{extra}")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn=Depends(get_conn)):
    return _settings_page(request, conn)


@app.post("/settings")
async def settings_save(request: Request, conn=Depends(get_conn)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    values, errors = settings_store.validate(form)
    if errors:
        page = _settings_page(request, conn, values=form, errors=errors)
        page.status_code = 400
        return page
    return _applied(conn, settings_store.save(conn, values, "Saved on the Settings page"), "Settings saved")


@app.post("/settings/reset")
def settings_reset(conn=Depends(get_conn)):
    changed, _ = settings_store.restore(conn, dict(db.DEFAULT_SETTINGS), "Reset to defaults")
    return _applied(conn, changed, "Reset to defaults")


@app.post("/settings/restore/{snap_id}")
def settings_restore(snap_id: int, conn=Depends(get_conn)):
    values = settings_store.snapshot_values(conn, snap_id)
    if values is None:
        raise HTTPException(404, "No such saved version")
    changed, errors = settings_store.restore(conn, values, f"Restored the version from #{snap_id}")
    if errors:
        return back("/settings", "That version has values outside today's limits: " + "; ".join(errors.values()))
    return _applied(conn, changed, f"Restored version #{snap_id}")


@app.post("/settings/presets")
def preset_save(name: str = Form(...), conn=Depends(get_conn)):
    name = " ".join(name.split())[:40]
    if not name:
        return back("/settings", "Give the preset a name")
    settings_store.save_preset(conn, name)
    return back("/settings", f"Current settings saved as “{name}”")


@app.post("/settings/presets/load")
def preset_load(name: str = Form(...), conn=Depends(get_conn)):
    values = settings_store.preset_values(conn, name)
    if values is None:
        raise HTTPException(404, "No such preset")
    changed, errors = settings_store.restore(conn, values, f"Loaded preset “{name}”")
    if errors:
        return back("/settings", f"“{name}” has values outside today's limits: " + "; ".join(errors.values()))
    return _applied(conn, changed, f"Loaded “{name}”")


@app.post("/settings/presets/delete")
def preset_delete(name: str = Form(...), conn=Depends(get_conn)):
    settings_store.delete_preset(conn, name)
    return back("/settings", f"Deleted preset “{name}”")


@app.get("/settings/export.json")
def settings_export(conn=Depends(get_conn)):
    body = json.dumps({"settings": settings_store.current(conn), "defaults": db.DEFAULT_SETTINGS,
                       "presets": settings_store.presets(conn)}, indent=2, ensure_ascii=False)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="cse-settings-{date.today()}.json"'})


# --- API -------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def api_status(conn=Depends(get_conn)) -> dict:
    return status(conn)
