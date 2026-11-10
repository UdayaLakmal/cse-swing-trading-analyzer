"""Holdings and P/L derived from the trades table (average-cost method).

Fees: a trade's own `fees` if given, else price x quantity x fee_rate.
Buy fees are part of the cost basis; sell fees reduce proceeds. Unrealised
P/L also deducts the fees you would pay to sell today, so it is what you
would actually take home.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class Holding:
    symbol: str
    name: str
    quantity: int
    cost: float  # total cost basis incl. buy fees
    entry_price: float  # average price paid, excl. fees
    first_day: str  # first buy of the current position
    last_close: float | None = None
    last_day: str | None = None
    stop_loss_pct: float = 10.0
    stop_price: float | None = None
    weight_pct: float | None = None
    fee_rate: float = 0.0112

    @property
    def avg_cost(self) -> float:
        return self.cost / self.quantity

    @property
    def break_even(self) -> float:
        """Price at which selling (after sell fees) returns the full cost."""
        return self.avg_cost / (1 - self.fee_rate)

    @property
    def market_value(self) -> float | None:
        return None if self.last_close is None else self.last_close * self.quantity

    @property
    def unrealised(self) -> float | None:
        mv = self.market_value
        return None if mv is None else mv * (1 - self.fee_rate) - self.cost

    @property
    def unrealised_pct(self) -> float | None:
        u = self.unrealised
        return None if u is None else 100 * u / self.cost

    @property
    def initial_stop(self) -> float:
        return self.entry_price * (1 - self.stop_loss_pct / 100)

    @property
    def effective_stop(self) -> float:
        """The stop only ratchets up: a stored trailing stop wins if higher."""
        return max(self.initial_stop, self.stop_price or 0)

    @property
    def below_stop(self) -> bool:
        return self.last_close is not None and self.last_close < self.effective_stop


@dataclass
class Portfolio:
    holdings: list[Holding] = field(default_factory=list)
    realised: dict[str, float] = field(default_factory=dict)
    fees_paid: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return sum(h.cost for h in self.holdings)

    @property
    def market_value(self) -> float:
        return sum(h.market_value or 0 for h in self.holdings)

    @property
    def unrealised(self) -> float:
        return sum(h.unrealised or 0 for h in self.holdings)

    @property
    def realised_total(self) -> float:
        return sum(self.realised.values())


def _settings(conn: sqlite3.Connection) -> dict[str, float]:
    return {k: float(v) for k, v in conn.execute("SELECT key, value FROM settings")}


def build_portfolio(conn: sqlite3.Connection) -> Portfolio:
    s = _settings(conn)
    fee_rate = s.get("fee_rate_pct", 1.12) / 100
    default_stop = s.get("stop_loss_pct", 10.0)

    pf = Portfolio()
    pos: dict[str, dict] = {}
    for t in conn.execute("SELECT * FROM trades ORDER BY day, id"):
        sym, qty, price = t["symbol"], t["quantity"], t["price"]
        fee = t["fees"] if t["fees"] is not None else price * qty * fee_rate
        pf.fees_paid += fee
        p = pos.setdefault(sym, {"qty": 0, "cost": 0.0, "paid": 0.0, "first": None})
        if t["side"] == "BUY":
            if p["qty"] == 0:
                p["first"] = t["day"]
            p["qty"] += qty
            p["cost"] += qty * price + fee
            p["paid"] += qty * price
        else:
            if qty > p["qty"]:
                pf.warnings.append(
                    f"{t['day']} {sym}: sold {qty} but only {p['qty']} held; "
                    "the extra is ignored (missing buy?)"
                )
                qty = p["qty"]
                if qty == 0:
                    continue
                fee = fee * qty / t["quantity"]
            avg_cost = p["cost"] / p["qty"]
            avg_paid = p["paid"] / p["qty"]
            pf.realised[sym] = pf.realised.get(sym, 0.0) + qty * price - fee - avg_cost * qty
            p["qty"] -= qty
            p["cost"] -= avg_cost * qty
            p["paid"] -= avg_paid * qty

    names = dict(conn.execute("SELECT symbol, name FROM stocks"))
    overrides = {r["symbol"]: r for r in conn.execute("SELECT * FROM position_settings")}
    for sym, p in sorted(pos.items()):
        if p["qty"] <= 0:
            continue
        o = overrides.get(sym)
        h = Holding(
            symbol=sym,
            name=names.get(sym, sym),
            quantity=p["qty"],
            cost=p["cost"],
            entry_price=p["paid"] / p["qty"],
            first_day=p["first"],
            stop_loss_pct=(o["stop_loss_pct"] if o and o["stop_loss_pct"] is not None else default_stop),
            stop_price=o["stop_price"] if o else None,
            fee_rate=fee_rate,
        )
        row = conn.execute(
            """SELECT p.close, p.day FROM prices_daily p JOIN stocks s USING (stock_id)
               WHERE s.symbol = ? ORDER BY p.day DESC LIMIT 1""",
            (sym,),
        ).fetchone()
        if row:
            h.last_close, h.last_day = row["close"], row["day"]
        else:
            pf.warnings.append(f"{sym}: no stored price yet")
        pf.holdings.append(h)

    total = pf.market_value
    max_w = s.get("max_weight_pct", 20.0)
    for h in pf.holdings:
        if total and h.market_value is not None:
            h.weight_pct = 100 * h.market_value / total
            if h.weight_pct > max_w:
                pf.warnings.append(f"{h.symbol} is {h.weight_pct:.0f}% of the portfolio (limit {max_w:.0f}%)")
    return pf
