"""Daily technical analysis: features, score, verdicts, trailing stops, signal log.

Rules set the verdict; every point of the score carries a plain-English
reason so the report can show exactly why a stock says Hold or Sell.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field

import pandas as pd

from . import indicators as ind
from .portfolio import Holding, build_portfolio

MIN_BARS = 30  # below this there is too little history to say anything
ADD_MIN_SCORE = 40  # a holding needs at least this score for Add
AVOID_MAX_SCORE = -20  # a watchlist or scan stock at or below this is Avoid

# Every score rule: id, group, what it looks at, points it can give.
# The Strategy page documents the score from this list and counts how often
# each rule fires, so keep it in step with score() below.
SCORE_RULES = [
    ("ma20", "Trend", "Close vs 20-day average", "+5 above, -5 below", 5),
    ("ma50", "Trend", "Close vs 50-day average", "+10 above, -10 below", 10),
    ("ma200", "Trend", "Close vs 200-day average", "+15 above, -15 below", 15),
    ("ma_cross", "Trend", "50-day vs 200-day average", "+10 if the 50-day is above (long-term uptrend), -10 if below", 10),
    ("rsi", "Momentum", "RSI (14)", "+10 at 50-75, 0 at 30-50, -5 above 75 (overbought), -10 below 30", 10),
    ("macd", "Momentum", "MACD vs its signal line", "+10 above, -10 below", 10),
    ("macd_cross", "Momentum", "MACD crossover in the last 3 days", "+5 up-cross, -5 down-cross, else nothing", 5),
    ("volume", "Volume", "Today's volume vs 20-day average", "+10 up day / -10 down day, only at 1.5x normal volume or more", 10),
    ("rs1m", "Relative strength", "1-month return vs the ASPI", "+7 beat, -7 lagged", 7),
    ("rs3m", "Relative strength", "3-month return vs the ASPI", "+8 beat, -8 lagged", 8),
    ("range52", "Levels", "Position in the 52-week range", "+5 within 5% of the high, -5 within 5% of the low", 5),
    ("break20", "Levels", "20-day breakout", "+5 close above the 20-day high, -5 below the 20-day low", 5),
]


@dataclass
class Features:
    symbol: str
    name: str
    day: str
    bars: int
    close: float
    prev_close: float | None
    change_pct: float | None
    ma20: float | None
    ma50: float | None
    ma200: float | None
    rsi: float | None
    macd_hist: float | None
    macd_cross: int  # +1 bullish cross in last 3 bars, -1 bearish, 0 none
    atr: float | None
    vol_ratio: float | None  # today's volume / 20-day average
    turnover20: float | None  # average daily LKR turnover, 20 days
    hi52: float
    lo52: float
    swing_hi20: float | None  # highest high of the 20 bars before today
    swing_lo20: float | None
    rs1m: float | None  # % return minus ASPI % return
    rs3m: float | None


@dataclass
class Component:
    group: str
    points: int
    reason: str
    rule: str = ""  # id from SCORE_RULES


@dataclass
class Signal:
    symbol: str
    name: str
    list: str  # holding, watch, scan
    verdict: str
    score: int
    close: float
    day: str
    change_pct: float | None
    stop: float | None = None
    target: float | None = None
    stop_stage: str | None = None
    reasons: list[str] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    size_shares: int | None = None
    features: Features | None = None
    rule: str = ""  # which verdict rule decided (HOLDING_RULES / ENTRY_RULES)
    checks: dict[str, bool] = field(default_factory=dict)  # entry checks and whether each passed


def _f(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else x


# --- features ------------------------------------------------------------


def compute_features(symbol: str, name: str, df: pd.DataFrame, index_close: pd.Series | None) -> Features | None:
    df = df.dropna(subset=["close"])
    if len(df) < MIN_BARS:
        return None
    close = df["close"]
    m = ind.macd(close)
    hist = m["hist"]
    cross = 0
    for i in range(1, min(4, len(hist))):
        a, b = hist.iloc[-i - 1], hist.iloc[-i]
        if pd.notna(a) and pd.notna(b):
            if a <= 0 < b:
                cross = 1
                break
            if a >= 0 > b:
                cross = -1
                break
    vol = df["volume"].astype(float)
    vol20 = ind.sma(vol, 20).shift(1).iloc[-1]  # average of the 20 days before today
    turnover = (close * vol).rolling(20, min_periods=10).mean().iloc[-1]
    last252 = df.iloc[-252:]

    def rs(n):
        mine = ind.pct_change(close, n)
        if mine is None or index_close is None:
            return None
        idx = index_close.reindex(close.index).ffill()
        theirs = ind.pct_change(idx, n)
        return None if theirs is None else mine - theirs

    prev = _f(close.iloc[-2]) if len(close) > 1 else None
    return Features(
        symbol=symbol,
        name=name,
        day=str(df.index[-1]),
        bars=len(df),
        close=float(close.iloc[-1]),
        prev_close=prev,
        change_pct=(100 * (close.iloc[-1] / prev - 1)) if prev else None,
        ma20=_f(ind.sma(close, 20).iloc[-1]),
        ma50=_f(ind.sma(close, 50).iloc[-1]),
        ma200=_f(ind.sma(close, 200).iloc[-1]),
        rsi=_f(ind.rsi(close).iloc[-1]),
        macd_hist=_f(hist.iloc[-1]),
        macd_cross=cross,
        atr=_f(ind.atr(df).iloc[-1]),
        vol_ratio=_f(vol.iloc[-1] / vol20) if vol20 and vol20 > 0 else None,
        turnover20=_f(turnover),
        hi52=float(last252["high"].fillna(last252["close"]).max()),
        lo52=float(last252["low"].fillna(last252["close"]).min()),
        swing_hi20=_f(df["high"].fillna(close).iloc[-21:-1].max()) if len(df) > 21 else None,
        swing_lo20=_f(df["low"].fillna(close).iloc[-21:-1].min()) if len(df) > 21 else None,
        rs1m=rs(21),
        rs3m=rs(63),
    )


# --- score ---------------------------------------------------------------


def score(f: Features) -> tuple[int, list[Component]]:
    """-100..+100 from trend, momentum, volume, relative strength and levels."""
    c: list[Component] = []

    def add(group, pts, reason, rule):
        c.append(Component(group, pts, reason, rule))

    # Trend (up to ±40)
    if f.ma20:
        add("Trend", 5 if f.close > f.ma20 else -5, f"{'Above' if f.close > f.ma20 else 'Below'} 20-day average ({f.ma20:,.2f})", "ma20")
    if f.ma50:
        add("Trend", 10 if f.close > f.ma50 else -10, f"{'Above' if f.close > f.ma50 else 'Below'} 50-day average ({f.ma50:,.2f})", "ma50")
    if f.ma200:
        add("Trend", 15 if f.close > f.ma200 else -15, f"{'Above' if f.close > f.ma200 else 'Below'} 200-day average ({f.ma200:,.2f})", "ma200")
        if f.ma50:
            up = f.ma50 > f.ma200
            add("Trend", 10 if up else -10, "50-day above 200-day (long-term uptrend)" if up else "50-day below 200-day (long-term downtrend)", "ma_cross")
    elif f.bars < 200:
        add("Trend", 0, f"Only {f.bars} days of history: no 200-day average yet", "history")

    # Momentum (up to ±25)
    if f.rsi is not None:
        r = f.rsi
        if r > 75:
            add("Momentum", -5, f"RSI {r:.0f}: overbought, pullback risk", "rsi")
        elif r >= 50:
            add("Momentum", 10, f"RSI {r:.0f}: positive momentum", "rsi")
        elif r >= 30:
            add("Momentum", 0, f"RSI {r:.0f}: neutral to soft", "rsi")
        else:
            add("Momentum", -10, f"RSI {r:.0f}: oversold, sellers in control", "rsi")
    if f.macd_hist is not None:
        add("Momentum", 10 if f.macd_hist > 0 else -10, "MACD above its signal line" if f.macd_hist > 0 else "MACD below its signal line", "macd")
    if f.macd_cross:
        add("Momentum", 5 * f.macd_cross, "MACD crossed up in the last 3 days" if f.macd_cross > 0 else "MACD crossed down in the last 3 days", "macd_cross")

    # Volume (±10)
    if f.vol_ratio is not None and f.vol_ratio >= 1.5 and f.change_pct is not None:
        if f.change_pct > 0:
            add("Volume", 10, f"Up day on {f.vol_ratio:.1f}x normal volume", "volume")
        elif f.change_pct < 0:
            add("Volume", -10, f"Down day on {f.vol_ratio:.1f}x normal volume", "volume")

    # Relative strength vs ASPI (±15)
    if f.rs1m is not None:
        add("Relative strength", 7 if f.rs1m > 0 else -7, f"{'Beat' if f.rs1m > 0 else 'Lagged'} the ASPI by {abs(f.rs1m):.1f} pts over 1 month", "rs1m")
    if f.rs3m is not None:
        add("Relative strength", 8 if f.rs3m > 0 else -8, f"{'Beat' if f.rs3m > 0 else 'Lagged'} the ASPI by {abs(f.rs3m):.1f} pts over 3 months", "rs3m")

    # Levels (±10)
    if f.hi52 and f.close >= 0.95 * f.hi52:
        add("Levels", 5, f"Within 5% of the 52-week high ({f.hi52:,.2f})", "range52")
    elif f.lo52 and f.close <= 1.05 * f.lo52:
        add("Levels", -5, f"Within 5% of the 52-week low ({f.lo52:,.2f})", "range52")
    if f.swing_hi20 and f.close > f.swing_hi20:
        add("Levels", 5, f"Broke above the 20-day high ({f.swing_hi20:,.2f})", "break20")
    elif f.swing_lo20 and f.close < f.swing_lo20:
        add("Levels", -5, f"Broke below the 20-day low ({f.swing_lo20:,.2f})", "break20")

    total = max(-100, min(100, sum(x.points for x in c)))
    return total, c


# --- stops & verdicts ----------------------------------------------------


def trailing_stop(h: Holding, highest: float, atr: float | None, s: dict) -> tuple[float, str]:
    """Stop for a holding per the plan: initial % stop, break-even after +1R,
    then a Chandelier trail (highest close since entry minus N x ATR). Never
    lower than a stop stored by an earlier run."""
    pct = h.stop_loss_pct / 100
    r = h.entry_price * pct
    stop, stage = h.entry_price * (1 - pct), "initial"
    if highest >= h.entry_price + s["breakeven_at_r"] * r:
        if h.break_even > stop:
            stop, stage = h.break_even, "break-even"
        if atr:
            chand = highest - s["chandelier_atr_mult"] * atr
            if chand > stop:
                stop, stage = chand, "trailing"
    if h.stop_price and h.stop_price > stop:
        stop, stage = h.stop_price, "trailing"
    return stop, stage


def holding_verdict(h: Holding, f: Features, sc: int, s: dict, *, highest: float,
                    sold_since_entry: bool, regime_ok: bool) -> Signal:
    stop, stage = trailing_stop(h, highest, f.atr, s)
    r = h.entry_price * h.stop_loss_pct / 100
    target = h.entry_price + s["partial_take_at_r"] * r
    reasons = []
    liquid = (f.turnover20 or 0) >= s["min_turnover_lkr"]
    if f.close < stop:
        verdict, rule = "Sell", "stop"
        reasons.append(f"Closed at {f.close:,.2f}, below the {stage} stop of {stop:,.2f}")
    elif f.close >= target and not sold_since_entry:
        verdict, rule = "Trim", "target"
        reasons.append(f"Reached +{s['partial_take_at_r']:g}R ({target:,.2f}): take about a third off and let the rest run on the trailing stop")
    elif h.weight_pct and h.weight_pct > s["max_weight_pct"]:
        verdict, rule = "Trim", "weight"
        reasons.append(f"{h.weight_pct:.0f}% of the portfolio, above your {s['max_weight_pct']:g}% limit")
    elif sc >= ADD_MIN_SCORE and f.close > h.entry_price and regime_ok and liquid:
        verdict, rule = "Add", "add"
        reasons.append(f"Strong score ({sc:+d}) and in profit; room below your {s['max_weight_pct']:g}% weight limit")
    else:
        verdict, rule = "Hold", "hold"
        cushion = 100 * (f.close / stop - 1)
        reasons.append(f"Above the {stage} stop of {stop:,.2f} ({cushion:.1f}% cushion)")
        if sc <= -40:
            reasons.append(f"Weak technicals ({sc:+d}): watch the stop closely")
    if not liquid:
        reasons.append(f"Thinly traded (avg LKR {f.turnover20 or 0:,.0f}/day): exits may move the price")
    return Signal(h.symbol, h.name, "holding", verdict, sc, f.close, f.day, f.change_pct,
                  stop=stop, target=target, stop_stage=stage, reasons=reasons, rule=rule)


def entry_verdict(f: Features, sc: int, s: dict, *, regime_ok: bool, capital: float,
                  target_entry: float | None = None, listname: str = "watch") -> Signal:
    pct = s["stop_loss_pct"] / 100
    stop = f.close * (1 - pct)
    target = f.close * (1 + s["partial_take_at_r"] * pct)
    reasons: list[str] = []
    liquid = (f.turnover20 or 0) >= s["min_turnover_lkr"]
    lo, hi = s["enter_rsi_low"], s["enter_rsi_high"]
    downtrend = f.ma200 is not None and f.ma50 is not None and f.close < f.ma200 and f.ma50 < f.ma200
    checks = {
        "above 50-day": f.ma50 is not None and f.close > f.ma50,
        f"RSI {lo:g}-{hi:g}": f.rsi is not None and lo <= f.rsi <= hi,
        "volume at or above normal": f.vol_ratio is not None and f.vol_ratio >= 1.0,
    }
    if not liquid:
        verdict, rule = "Avoid", "illiquid"
        reasons.append(f"Thin trading: average LKR {f.turnover20 or 0:,.0f}/day, below your {s['min_turnover_lkr']:,.0f} minimum")
    elif downtrend:
        verdict, rule = "Avoid", "downtrend"
        reasons.append("Downtrend: below the 200-day average and the 50-day is under the 200-day")
    elif sc <= AVOID_MAX_SCORE:
        verdict, rule = "Avoid", "weak"
        reasons.append(f"Weak score ({sc:+d})")
    elif all(checks.values()):
        if regime_ok:
            verdict, rule = "Enter", "enter"
            reasons.append(f"Above the 50-day, RSI {f.rsi:.0f} after a pullback, volume {f.vol_ratio:.1f}x normal")
        else:
            verdict, rule = "Wait", "regime"
            reasons.append("Setup is in place, but the ASPI is below its 200-day average: buys fail more often in a falling market")
    else:
        verdict, rule = "Wait", "missing"
        missing = [k for k, ok in checks.items() if not ok]
        reasons.append("Waiting for: " + ", ".join(missing))
        if f.rsi is not None and f.rsi > hi:
            reasons.append(f"RSI {f.rsi:.0f} is stretched: wait for a pullback")
    if target_entry:
        gap = 100 * (f.close / target_entry - 1)
        reasons.append(f"Your target entry {target_entry:,.2f} is {abs(gap):.1f}% {'below' if gap > 0 else 'above'} the close")
    size = None
    if capital > 0 and f.close > stop:
        risk_lkr = capital * s["risk_per_trade_pct"] / 100
        size = int(risk_lkr / (f.close - stop))
        size = min(size, int(capital * s["max_weight_pct"] / 100 / f.close))
    return Signal(f.symbol, f.name, listname, verdict, sc, f.close, f.day, f.change_pct,
                  stop=stop, target=target, stop_stage="suggested", reasons=reasons, size_shares=size,
                  rule=rule, checks=checks)


# --- run -----------------------------------------------------------------


def _settings(conn) -> dict[str, float]:
    return {k: float(v) for k, v in conn.execute("SELECT key, value FROM settings")}


def market_regime(conn) -> dict:
    rows = conn.execute("SELECT day, value FROM index_daily WHERE index_code = 'ASPI' ORDER BY day").fetchall()
    if not rows:
        return {"ok": True, "known": False}
    s = pd.Series([r[1] for r in rows], index=[r[0] for r in rows])
    ma = ind.sma(s, 200).iloc[-1]
    last = float(s.iloc[-1])
    prev = float(s.iloc[-2]) if len(s) > 1 else None
    out = {"day": s.index[-1], "aspi": last, "change_pct": (100 * (last / prev - 1)) if prev else None,
           "ma200": _f(ma), "known": not pd.isna(ma)}
    out["ok"] = True if pd.isna(ma) else bool(last >= ma)
    return out


def load_prices(conn) -> dict[str, tuple[str, pd.DataFrame]]:
    df = pd.read_sql_query(
        """SELECT s.symbol, s.name, p.day, p.high, p.low, p.close, p.volume
           FROM prices_daily p JOIN stocks s USING (stock_id) ORDER BY s.symbol, p.day""",
        conn,
    )
    out = {}
    for sym, g in df.groupby("symbol", sort=False):
        out[sym] = (g["name"].iloc[0], g.set_index("day")[["high", "low", "close", "volume"]])
    return out


def run_analysis(conn: sqlite3.Connection) -> dict:
    s = _settings(conn)
    regime = market_regime(conn)
    aspi = pd.Series(dict(conn.execute("SELECT day, value FROM index_daily WHERE index_code='ASPI' ORDER BY day").fetchall()), dtype=float)
    prices = load_prices(conn)
    pf = build_portfolio(conn)
    capital = pf.market_value
    watch = {r["symbol"]: r["target_entry"] for r in conn.execute("SELECT symbol, target_entry FROM watchlist")}
    held = {h.symbol: h for h in pf.holdings}

    signals: list[Signal] = []
    feats: dict[str, Features] = {}
    for sym, (name, df) in prices.items():
        f = compute_features(sym, name, df, aspi if len(aspi) else None)
        if f is None:
            continue
        feats[sym] = f
        sc, comps = score(f)
        if sym in held:
            h = held[sym]
            since = df.loc[df.index >= h.first_day, "close"]
            highest = float(since.max()) if len(since) else f.close
            sold = conn.execute(
                "SELECT 1 FROM trades WHERE symbol = ? AND side = 'SELL' AND day >= ? LIMIT 1", (sym, h.first_day)
            ).fetchone() is not None
            sig = holding_verdict(h, f, sc, s, highest=highest, sold_since_entry=sold, regime_ok=regime["ok"])
            # Ratchet: remember the stop so it can never move down later.
            if sig.stop_stage in ("break-even", "trailing"):
                conn.execute(
                    """INSERT INTO position_settings (symbol, stop_price, stop_entry_day) VALUES (?, ?, ?)
                       ON CONFLICT(symbol) DO UPDATE SET
                         stop_price = CASE WHEN position_settings.stop_entry_day IS excluded.stop_entry_day
                                           THEN MAX(COALESCE(position_settings.stop_price, 0), excluded.stop_price)
                                           ELSE excluded.stop_price END,
                         stop_entry_day = excluded.stop_entry_day""",
                    (sym, sig.stop, h.first_day),
                )
        else:
            sig = entry_verdict(f, sc, s, regime_ok=regime["ok"], capital=capital,
                                target_entry=watch.get(sym), listname="watch" if sym in watch else "scan")
        sig.components, sig.features = comps, f
        signals.append(sig)

    for sym in set(held) | set(watch):
        if sym not in feats:
            name = held[sym].name if sym in held else sym
            signals.append(Signal(sym, name, "holding" if sym in held else "watch", "No data", 0, 0.0, "", None,
                                  reasons=[f"Fewer than {MIN_BARS} days of prices stored"]))

    day = max((x.day for x in signals if x.day), default=None)
    if day:
        conn.execute("DELETE FROM signals WHERE day = ?", (day,))
        conn.executemany(
            """INSERT INTO signals (day, symbol, list, verdict, score, close, stop, target, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(day, x.symbol, x.list, x.verdict, x.score, x.close, x.stop, x.target,
              json.dumps({"reasons": x.reasons, "components": [asdict(c) for c in x.components],
                          "stop_stage": x.stop_stage, "size_shares": x.size_shares, "name": x.name,
                          "change_pct": x.change_pct, "bar_day": x.day, "rule": x.rule, "checks": x.checks,
                          "features": asdict(x.features) if x.features else None}))
             for x in signals],
        )
    conn.commit()
    counts: dict[str, int] = {}
    for x in signals:
        if x.list != "scan":
            counts[f"{x.list}:{x.verdict}"] = counts.get(f"{x.list}:{x.verdict}", 0) + 1
    return {"day": day, "analysed": len(feats), "regime_ok": regime["ok"], "counts": counts}


def load_signals(conn, day: str | None = None) -> tuple[str | None, list[dict]]:
    if day is None:
        day = conn.execute("SELECT MAX(day) FROM signals").fetchone()[0]
    if day is None:
        return None, []
    out = []
    for r in conn.execute("SELECT * FROM signals WHERE day = ?", (day,)):
        d = dict(r)
        d.update(json.loads(d.pop("detail_json")))
        out.append(d)
    return day, out
