"""Trade input: symbol normalising, paste parsing, xlsx import/template, saving."""

from __future__ import annotations

import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import Workbook, load_workbook


@dataclass
class TradeRow:
    day: date
    symbol: str
    side: str  # BUY or SELL
    quantity: int
    price: float
    fees: float | None = None
    note: str | None = None


@dataclass
class ParseResult:
    rows: list[TradeRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- symbols ---------------------------------------------------------------


class SymbolResolver:
    """Maps user input like 'jkh', 'JKH.N', 'COMB.X0000' to a listed CSE symbol."""

    def __init__(self, symbols: list[str]):
        self._known = {s.upper() for s in symbols}

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> SymbolResolver:
        return cls([r[0] for r in conn.execute("SELECT symbol FROM stocks")])

    def resolve(self, raw: str) -> str | None:
        s = raw.strip().upper()
        if not s:
            return None
        if s in self._known:
            return s
        if "." in s:
            base, cls_ = s.split(".", 1)
            candidate = f"{base}.{cls_[:1]}0000"
            return candidate if candidate in self._known else None
        # Bare ticker: the voting (N) share is what people normally mean.
        for suffix in ("N0000", "X0000", "U0000"):
            if f"{s}.{suffix}" in self._known:
                return f"{s}.{suffix}"
        return None


# --- paste -----------------------------------------------------------------

_SIDES = {"B": "BUY", "BUY": "BUY", "BOUGHT": "BUY", "S": "SELL", "SELL": "SELL", "SOLD": "SELL"}
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b")
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
# "<SYMBOL> <QTY> @ <PRICE>", "@" optional, side word anywhere.
_TRADE_RE = re.compile(rf"([A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z]\d{{0,4}})?)\s+{_NUM}\s*(?:@|x|at)?\s*{_NUM}", re.I)


def _parse_date(s: str) -> date:
    if "-" in s:
        return date.fromisoformat(s)
    d, m, y = (int(p) for p in s.split("/"))
    return date(y, m, d)  # Sri Lankan convention: day/month/year


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_paste(text: str, resolver: SymbolResolver, default_day: date) -> ParseResult:
    """Parse one trade per line, e.g.:

        JKH 500 @ 195.50
        SELL COMB 100 @ 120 2026-11-01
        2026-11-03 B HNB.N0000 1,000 @ 310
        BUY JKH 500 @ 195.5 fees 1100

    Side defaults to BUY. Date defaults to `default_day`. Lines starting with #
    are ignored.
    """
    result = ParseResult()
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            day = default_day
            if m := _DATE_RE.search(line):
                day = _parse_date(m.group(1))
                line_wo_date = line[: m.start()] + " " + line[m.end() :]
            else:
                line_wo_date = line

            fees = None
            if m := re.search(rf"\bfees?\s*:?\s*{_NUM}", line_wo_date, re.I):
                fees = _num(m.group(1))
                line_wo_date = line_wo_date[: m.start()] + line_wo_date[m.end() :]

            side = "BUY"
            tokens = line_wo_date.split()
            for i, t in enumerate(tokens):
                if t.upper() in _SIDES:
                    side = _SIDES[t.upper()]
                    del tokens[i]
                    break

            m = _TRADE_RE.search(" ".join(tokens))
            if not m:
                raise ValueError("expected SYMBOL QTY @ PRICE")
            raw_sym, qty_s, price_s = m.groups()
            symbol = resolver.resolve(raw_sym)
            if not symbol:
                raise ValueError(f"unknown symbol {raw_sym!r}")
            qty = _num(qty_s)
            if qty != int(qty) or qty <= 0:
                raise ValueError("quantity must be a whole number above 0")
            price = _num(price_s)
            if price <= 0:
                raise ValueError("price must be above 0")
            result.rows.append(TradeRow(day, symbol, side, int(qty), price, fees))
        except ValueError as e:
            result.errors.append(f"line {n}: {e}: {line}")
    return result


# --- xlsx ------------------------------------------------------------------

XLSX_COLUMNS = ["Date", "Symbol", "Side", "Quantity", "Price", "Fees", "Note"]


def xlsx_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Trades"
    ws.append(XLSX_COLUMNS)
    ws.append([date(2026, 11, 3), "JKH", "BUY", 500, 19.5, None, "example row, delete me"])
    for col, width in zip("ABCDEFG", (12, 14, 8, 10, 10, 10, 30)):
        ws.column_dimensions[col].width = width
    ws["A2"].number_format = "yyyy-mm-dd"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_xlsx(data: bytes, resolver: SymbolResolver) -> ParseResult:
    result = ParseResult()
    try:
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    except Exception as e:  # noqa: BLE001 - openpyxl raises many types
        result.errors.append(f"could not read the file as .xlsx: {e}")
        return result

    rows = ws.iter_rows(values_only=True)
    header = [str(c).strip().lower() if c is not None else "" for c in next(rows, [])]
    idx = {name: header.index(name.lower()) for name in XLSX_COLUMNS if name.lower() in header}
    missing = [c for c in XLSX_COLUMNS[:5] if c not in idx]
    if missing:
        result.errors.append(f"missing columns: {', '.join(missing)} (download the template)")
        return result

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    for n, row in enumerate(rows, 2):
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        try:
            raw_day = cell(row, "Date")
            if isinstance(raw_day, datetime):
                day = raw_day.date()
            elif isinstance(raw_day, date):
                day = raw_day
            else:
                day = _parse_date(str(raw_day).strip())
            raw_sym = str(cell(row, "Symbol") or "")
            symbol = resolver.resolve(raw_sym)
            if not symbol:
                raise ValueError(f"unknown symbol {raw_sym!r}")
            side = _SIDES.get(str(cell(row, "Side") or "").strip().upper())
            if not side:
                raise ValueError("Side must be BUY or SELL")
            qty = float(cell(row, "Quantity"))
            if qty != int(qty) or qty <= 0:
                raise ValueError("Quantity must be a whole number above 0")
            price = float(cell(row, "Price"))
            if price <= 0:
                raise ValueError("Price must be above 0")
            fees = cell(row, "Fees")
            note = cell(row, "Note")
            result.rows.append(
                TradeRow(day, symbol, side, int(qty), price,
                         float(fees) if fees not in (None, "") else None,
                         str(note) if note else None)
            )
        except (ValueError, TypeError) as e:
            result.errors.append(f"row {n}: {e}")
    return result


# --- saving ----------------------------------------------------------------


def save_trades(conn: sqlite3.Connection, rows: list[TradeRow], source: str) -> tuple[int, int]:
    """Insert trades; exact duplicates (same day, symbol, side, qty, price) are skipped.

    Returns (added, skipped). This is what makes re-uploading a running
    spreadsheet safe.
    """
    added = 0
    for r in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO trades (day, symbol, side, quantity, price, fees, note, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.day.isoformat(), r.symbol, r.side, r.quantity, r.price, r.fees, r.note, source),
        )
        added += cur.rowcount
    conn.commit()
    return added, len(rows) - added
