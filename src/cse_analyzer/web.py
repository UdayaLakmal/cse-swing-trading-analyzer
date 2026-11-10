"""Web app: holdings, trades (form, paste, xlsx) and watchlist pages."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import collector, db
from .cli import status
from .config import load_settings
from .portfolio import build_portfolio
from .trades import SymbolResolver, TradeRow, parse_paste, parse_xlsx, save_trades, xlsx_template

app = FastAPI(title="CSE Swing-Trading Analyzer")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def money(v: float | None, digits: int = 2) -> str:
    return "–" if v is None else f"{v:,.{digits}f}"


def signed(v: float | None, digits: int = 2) -> str:
    return "–" if v is None else f"{v:+,.{digits}f}"


templates.env.filters["money"] = money
templates.env.filters["signed"] = signed


def get_conn() -> Iterator:
    conn = db.connect(load_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


def render(request: Request, name: str, conn, **ctx) -> HTMLResponse:
    latest = collector.latest_price_day(conn)
    ctx.update(
        request=request,
        stale=collector.is_stale(conn),
        latest_day=latest.isoformat() if latest else None,
        symbols=[r[0] for r in conn.execute("SELECT symbol FROM stocks ORDER BY symbol")],
        today=date.today().isoformat(),
    )
    return templates.TemplateResponse(request, name, ctx)


def back(path: str, msg: str | None = None) -> RedirectResponse:
    url = path if not msg else f"{path}?msg={msg}"
    return RedirectResponse(url, status_code=303)


# --- pages -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def holdings_page(request: Request, conn=Depends(get_conn), msg: str | None = None):
    return render(request, "holdings.html", conn, pf=build_portfolio(conn), msg=msg)


@app.post("/holdings/{symbol}/stop")
def set_stop(symbol: str, stop_loss_pct: str = Form(""), conn=Depends(get_conn)):
    value = float(stop_loss_pct) if stop_loss_pct.strip() else None
    if value is not None and not 0 < value < 100:
        raise HTTPException(400, "stop-loss % must be between 0 and 100")
    conn.execute(
        """INSERT INTO position_settings (symbol, stop_loss_pct) VALUES (?, ?)
           ON CONFLICT(symbol) DO UPDATE SET stop_loss_pct = excluded.stop_loss_pct""",
        (symbol, value),
    )
    conn.commit()
    return back("/", f"Stop-loss for {symbol} updated")


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, conn=Depends(get_conn), msg: str | None = None):
    rows = conn.execute("SELECT * FROM trades ORDER BY day DESC, id DESC").fetchall()
    return render(request, "trades.html", conn, trades=rows, msg=msg)


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
    return back("/trades", f"Saved {side} {quantity} {sym}" if added else "That exact trade is already saved")


@app.post("/trades/paste", response_class=HTMLResponse)
def paste_preview(request: Request, text: str = Form(...), conn=Depends(get_conn)):
    result = parse_paste(text, SymbolResolver.from_db(conn), date.today())
    return _preview(request, conn, result, "paste")


@app.post("/trades/upload", response_class=HTMLResponse)
async def upload_preview(request: Request, file: UploadFile = File(...), conn=Depends(get_conn)):
    fee_rate = float(conn.execute("SELECT value FROM settings WHERE key = 'fee_rate_pct'").fetchone()[0]) / 100
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
    return render(request, "preview.html", conn, rows=rows, errors=result.errors, notes=notes,
                  payload=payload, source=source, kind=result.kind)


@app.post("/trades/confirm")
def confirm(payload: str = Form(...), source: str = Form(...), conn=Depends(get_conn)):
    rows = [
        TradeRow(date.fromisoformat(r["day"]), r["symbol"], r["side"], int(r["quantity"]),
                 float(r["price"]), r.get("fees"), r.get("note"))
        for r in json.loads(payload)
    ]
    added, skipped = save_trades(conn, rows, source if source in ("paste", "xlsx", "statement") else "form")
    return back("/trades", f"Saved {added} trades" + (f", skipped {skipped} duplicates" if skipped else ""))


@app.post("/trades/{trade_id}/delete")
def delete_trade(trade_id: int, conn=Depends(get_conn)):
    conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    conn.commit()
    return back("/trades", "Trade deleted")


@app.get("/trades/template.xlsx")
def template():
    return Response(
        xlsx_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="cse-trades-template.xlsx"'},
    )


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, conn=Depends(get_conn), msg: str | None = None):
    rows = conn.execute(
        """SELECT w.*, s.name,
                  (SELECT p.close FROM prices_daily p WHERE p.stock_id = s.stock_id ORDER BY p.day DESC LIMIT 1) AS last_close
           FROM watchlist w LEFT JOIN stocks s USING (symbol) ORDER BY w.symbol"""
    ).fetchall()
    return render(request, "watchlist.html", conn, items=rows, msg=msg)


@app.post("/watchlist/add")
def watchlist_add(symbol: str = Form(...), target_entry: str = Form(""), note: str = Form(""), conn=Depends(get_conn)):
    sym = SymbolResolver.from_db(conn).resolve(symbol)
    if not sym:
        return back("/watchlist", f"Unknown symbol {symbol}")
    conn.execute(
        """INSERT INTO watchlist (symbol, target_entry, note) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET target_entry = excluded.target_entry, note = excluded.note""",
        (sym, float(target_entry) if target_entry.strip() else None, note.strip() or None),
    )
    conn.commit()
    return back("/watchlist", f"{sym} saved")


@app.post("/watchlist/{symbol}/delete")
def watchlist_delete(symbol: str, conn=Depends(get_conn)):
    conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
    conn.commit()
    return back("/watchlist", f"{symbol} removed")


# --- api -------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def api_status(conn=Depends(get_conn)) -> dict:
    return status(conn)
