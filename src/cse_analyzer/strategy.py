"""Strategy page: how the score and verdicts are worked out, measured against
today's signals, so a rule that misbehaves or a setting that is too tight is
easy to spot.

Everything here reads the stored signal log; nothing changes the database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from . import analysis
from .analysis import ADD_MIN_SCORE, AVOID_MAX_SCORE, SCORE_RULES
from .portfolio import build_portfolio
from .trades import STATEMENT_NOTE

GROUPS = ["Trend", "Momentum", "Volume", "Relative strength", "Levels"]
GROUP_MAX = {g: sum(r[4] for r in SCORE_RULES if r[1] == g) for g in GROUPS}
RULE_LABELS = {
    "stop": "Below the stop", "target": "Profit target", "weight": "Over weight limit", "add": "Strong and in profit",
    "hold": "Above the stop", "illiquid": "Thin trading", "downtrend": "Downtrend", "weak": "Weak score",
    "enter": "All checks pass", "regime": "Market filter", "missing": "Checks missing",
}
BUCKETS = [(lo, lo + 20) for lo in range(-100, 100, 20)]


def holding_rules(s: dict) -> list[tuple[str, str, str]]:
    """(rule id, verdict, condition) checked top to bottom; the first match wins."""
    return [
        ("stop", "Sell", "Close is below the stop (initial, break-even or trailing)"),
        ("target", "Trim", f"Close reached +{s['partial_take_at_r']:g}R and nothing was sold since entry: take about a third"),
        ("weight", "Trim", f"Holding is more than {s['max_weight_pct']:g}% of the portfolio"),
        ("add", "Add", f"Score {ADD_MIN_SCORE:+d} or more, in profit, market uptrend and turnover at least LKR {s['min_turnover_lkr']:,.0f}/day"),
        ("hold", "Hold", "None of the above"),
    ]


def entry_rules(s: dict) -> list[tuple[str, str, str]]:
    return [
        ("illiquid", "Avoid", f"Average turnover below LKR {s['min_turnover_lkr']:,.0f}/day"),
        ("downtrend", "Avoid", "Below the 200-day average and the 50-day is under the 200-day"),
        ("weak", "Avoid", f"Score {AVOID_MAX_SCORE:+d} or lower"),
        ("enter", "Enter", f"Above the 50-day, RSI {s['enter_rsi_low']:g}-{s['enter_rsi_high']:g}, volume at or above normal, and the ASPI above its 200-day"),
        ("regime", "Wait", "All entry checks pass, but the ASPI is below its 200-day average"),
        ("missing", "Wait", "One or more entry checks fail (the reason lists which)"),
    ]


def _comps(sig: dict) -> list[dict]:
    return sig.get("components") or []


def _group_points(sig: dict) -> dict[str, int]:
    out = dict.fromkeys(GROUPS, 0)
    for c in _comps(sig):
        out[c["group"]] = out.get(c["group"], 0) + c["points"]
    return out


def rule_stats(scored: list[dict]) -> list[dict]:
    """How often each score rule gave plus, minus or nothing across the market."""
    rows = []
    n = len(scored) or 1
    for rid, group, what, points, mx in SCORE_RULES:
        pos = neg = zero = 0
        total = 0
        for sig in scored:
            hits = [c for c in _comps(sig) if c.get("rule") == rid]
            p = sum(c["points"] for c in hits)
            total += p
            if not hits or p == 0:
                zero += 1
            elif p > 0:
                pos += 1
            else:
                neg += 1
        rows.append({"id": rid, "group": group, "what": what, "points": points, "max": mx,
                     "pos": pos, "neg": neg, "zero": zero, "avg": total / n,
                     "pos_pct": 100 * pos / n, "neg_pct": 100 * neg / n,
                     # a rule that is the same sign for almost everyone barely separates stocks
                     "one_sided": len(scored) >= 20 and max(pos, neg) / n >= 0.85})
    return rows


def distribution(scored: list[dict]) -> list[dict]:
    rows = []
    for lo, hi in BUCKETS:
        inb = [x for x in scored if lo <= x["score"] < hi or (hi == 100 and x["score"] == 100)]
        rows.append({"lo": lo, "hi": hi, "n": len(inb), "mine": sum(1 for x in inb if x["list"] != "scan")})
    peak = max((r["n"] for r in rows), default=0) or 1
    for r in rows:
        r["h"] = 100 * r["n"] / peak
    return rows


def _highest_and_sold(conn, symbol: str, since: str) -> tuple[float | None, bool]:
    hi = conn.execute(
        """SELECT MAX(p.close) FROM prices_daily p JOIN stocks s USING (stock_id)
           WHERE s.symbol = ? AND p.day >= ?""", (symbol, since)).fetchone()[0]
    sold = conn.execute("SELECT 1 FROM trades WHERE symbol = ? AND side = 'SELL' AND day >= ? LIMIT 1",
                        (symbol, since)).fetchone() is not None
    return hi, sold


def stop_ladder(conn, holdings, by_sym: dict, s: dict) -> list[dict]:
    """Each holding's exit levels: where the stop is now and what moves it."""
    rows = []
    for h in holdings:
        sig = by_sym.get(h.symbol)
        if not sig or not sig.get("features"):
            continue
        highest, _ = _highest_and_sold(conn, h.symbol, h.first_day)
        r = h.entry_price * h.stop_loss_pct / 100
        close = sig["close"]
        from_statement = conn.execute(
            "SELECT COUNT(*) = SUM(note = ?) FROM trades WHERE symbol = ? AND side = 'BUY' AND day >= ?",
            (STATEMENT_NOTE, h.symbol, h.first_day)).fetchone()[0]
        rows.append({
            "symbol": h.symbol, "name": h.name, "verdict": sig["verdict"], "close": close,
            "entry": h.entry_price, "first_day": h.first_day, "stop_pct": h.stop_loss_pct,
            "initial": h.entry_price - r, "be_trigger": h.entry_price + s["breakeven_at_r"] * r,
            "break_even": h.break_even, "target": h.entry_price + s["partial_take_at_r"] * r,
            "stop": sig.get("stop"), "stage": sig.get("stop_stage"), "highest": highest,
            "move_pct": 100 * (close / h.entry_price - 1), "r_multiple": (close - h.entry_price) / r if r else None,
            "cushion": 100 * (close / sig["stop"] - 1) if sig.get("stop") else None,
            "from_statement": bool(from_statement),
        })
    return rows


def what_if(conn, sigs: list[dict], s: dict, regime_ok: bool, holdings) -> dict:
    """Re-run the verdict rules on today's stored features with one setting
    changed at a time. Scores do not depend on these settings, so only the
    verdicts move."""
    entries = [x for x in sigs if x["list"] in ("watch", "scan") and x.get("features")]
    feats = [(x, analysis.Features(**x["features"])) for x in entries]

    def count_entries(**over):
        ss = s | {k: v for k, v in over.items() if k != "regime_ok"}
        ok = over.get("regime_ok", regime_ok)
        out = {"Enter": 0, "Wait": 0, "Avoid": 0, "watch_enter": 0}
        for x, f in feats:
            v = analysis.entry_verdict(f, x["score"], ss, regime_ok=ok, capital=0, listname=x["list"]).verdict
            out[v] += 1
            out["watch_enter"] += v == "Enter" and x["list"] == "watch"
        return out

    lo, hi, mt = s["enter_rsi_low"], s["enter_rsi_high"], s["min_turnover_lkr"]
    entry_rows = [("Current settings", count_entries(), True)]
    entry_rows.append((f"RSI zone {lo - 5:g}-{hi + 5:g} instead of {lo:g}-{hi:g}",
                       count_entries(enter_rsi_low=lo - 5, enter_rsi_high=hi + 5), False))
    entry_rows.append((f"Minimum turnover LKR {mt / 2:,.0f} (half)", count_entries(min_turnover_lkr=mt / 2), False))
    entry_rows.append((f"Minimum turnover LKR {mt * 2:,.0f} (double)", count_entries(min_turnover_lkr=mt * 2), False))
    if not regime_ok:
        entry_rows.append(("Ignore the market downtrend", count_entries(regime_ok=True), False))

    by_sym = {x["symbol"]: x for x in sigs}
    held = []
    for h in holdings:
        sig = by_sym.get(h.symbol)
        if not sig or not sig.get("features"):
            continue
        highest, sold = _highest_and_sold(conn, h.symbol, h.first_day)
        held.append((h, analysis.Features(**sig["features"]), sig["score"], highest or sig["close"], sold))

    def count_holdings(pct):
        out = {"Sell": 0, "Trim": 0, "Add": 0, "Hold": 0}
        for h, f, sc, highest, sold in held:
            hh = replace(h, stop_loss_pct=pct, stop_price=None) if pct is not None else h
            out[analysis.holding_verdict(hh, f, sc, s, highest=highest, sold_since_entry=sold,
                                         regime_ok=regime_ok).verdict] += 1
        return out

    hold_rows = [("Current stops", count_holdings(None), True)]
    for pct in (15, 20, 25):
        if pct != s["stop_loss_pct"]:
            hold_rows.append((f"Every holding on a {pct}% stop", count_holdings(pct), False))
    return {"entries": entry_rows, "holdings": hold_rows, "n_entries": len(feats), "n_holdings": len(held)}


def hints(sigs, ladder, rstats, missing, s, regime) -> list[dict]:
    """Plain observations worth a look; each points at the setting involved."""
    out = []
    held = [x for x in sigs if x["list"] == "holding"]
    sells = [r for r in ladder if r["verdict"] == "Sell"]
    if sells:
        stmt = [r for r in sells if r["from_statement"]]
        text = f"{len(sells)} of {len(held)} holdings are below their stop."
        if stmt:
            text += (f" {len(stmt)} of them came from the broker statement, so the stop is measured from your average cost "
                     f"and the trail starts on {stmt[0]['first_day']}, not from when you really bought. If these are long-term "
                     "positions you mean to keep, give them their own stop % on the Holdings page.")
        out.append({"level": "warn", "text": text, "href": "/holdings", "link": "Set per-stock stops"})
    near = [r for r in ladder if r["verdict"] != "Sell" and r["cushion"] is not None and r["cushion"] < 3]
    if near:
        out.append({"level": "warn", "text": "Within 3% of the stop: " + ", ".join(r["symbol"].split(".")[0] for r in near) + ".",
                    "href": None, "link": None})
    weak_holds = [x for x in held if x["verdict"] == "Hold" and x["score"] <= -40]
    if weak_holds:
        out.append({"level": "info", "text": "Held above the stop but scoring -40 or worse: "
                    + ", ".join(x["symbol"].split(".")[0] for x in weak_holds)
                    + ". The stop decides the exit; a weak score alone never says Sell.", "href": None, "link": None})

    entries = [x for x in sigs if x["list"] in ("watch", "scan")]
    if entries:
        thin = sum(1 for x in entries if x.get("rule") == "illiquid")
        if thin / len(entries) >= 0.4:
            out.append({"level": "info", "text": f"{thin} of {len(entries)} stocks ({100 * thin / len(entries):.0f}%) are Avoid only "
                        f"because they trade less than LKR {s['min_turnover_lkr']:,.0f} a day.",
                        "href": "/settings", "link": "Minimum turnover"})
        enters = sum(1 for x in entries if x["verdict"] == "Enter")
        only = max(missing, key=lambda m: m["only"]) if missing else None
        if missing and (enters == 0 or only["only"] >= 5):
            top = max(missing, key=lambda m: m["fails"])
            text = (f"No Enter signals today. " if enters == 0 else f"{enters} Enter signal(s) today. ") + \
                f"The entry check that fails most is “{top['check']}” ({top['fails']} stocks)"
            if only["only"]:
                text += f", and {only['only']} stocks miss only “{only['check']}”"
            if only.get("above") and only["above"] * 2 > only["only"]:
                text += (f"; {only['above']} of those have RSI above {s['enter_rsi_high']:g}, strong stocks that have not "
                         "pulled back yet, which is the rule doing its job")
            out.append({"level": "info", "text": text + ".", "href": "/settings", "link": "Entry settings"})
        weak_enter = [x for x in entries if x["verdict"] == "Enter" and x["score"] < 20]
        if weak_enter:
            out.append({"level": "info", "text": f"{len(weak_enter)} Enter signal(s) have a score under +20: the entry checks pass "
                        "but the wider picture is mixed. Look at their stock pages before acting.", "href": "/scan", "link": "Scan"})
    muted = sum(1 for x in sigs if x.get("rule") == "regime")
    if muted:
        out.append({"level": "info", "text": f"{muted} setups are muted to Wait because the ASPI is below its 200-day average.",
                    "href": None, "link": None})
    flat = [r for r in rstats if r["one_sided"]]
    if flat:
        out.append({"level": "info", "text": "Rules giving the same sign to 85% or more of stocks today: "
                    + ", ".join(r["what"] for r in flat)
                    + ". That is normal in a strong trend, but they add little to tell stocks apart.", "href": None, "link": None})
    if not out:
        out.append({"level": "ok", "text": "Nothing unusual in today's signals.", "href": None, "link": None})
    return out


def build(conn: sqlite3.Connection, s: dict) -> dict:
    day, sigs = analysis.load_signals(conn)
    regime = analysis.market_regime(conn)
    scored = [x for x in sigs if x["verdict"] != "No data" and _comps(x)]
    pf = build_portfolio(conn)
    by_sym = {x["symbol"]: x for x in sigs}
    tagged = all(x.get("rule") for x in scored)  # older signals have no rule ids until re-run

    mine = [x for x in sigs if x["list"] in ("holding", "watch")]
    order = {"holding": 0, "watch": 1}
    mine.sort(key=lambda x: (order[x["list"]], analysis_order(x["verdict"]), -x["score"]))
    matrix = [{"sig": x, "groups": _group_points(x)} for x in mine]

    hrules = holding_rules(s)
    erules = entry_rules(s)
    hcount = {rid: sum(1 for x in sigs if x["list"] == "holding" and x.get("rule") == rid) for rid, *_ in hrules}
    ecount = {rid: {"watch": sum(1 for x in sigs if x["list"] == "watch" and x.get("rule") == rid),
                    "scan": sum(1 for x in sigs if x["list"] == "scan" and x.get("rule") == rid)} for rid, *_ in erules}

    waiting = [x for x in sigs if x.get("rule") == "missing" and x.get("checks")]
    keys = list(dict.fromkeys(k for x in waiting for k in x["checks"]))
    missing = [{"check": k,
                "fails": sum(1 for x in waiting if not x["checks"].get(k)),
                "only": sum(1 for x in waiting if not x["checks"].get(k) and sum(not v for v in x["checks"].values()) == 1)}
               for k in keys]
    for m in missing:  # for the RSI check, split near misses into stretched (above) and weak (below)
        if m["check"].startswith("RSI"):
            near = [x for x in waiting if not x["checks"].get(m["check"]) and sum(not v for v in x["checks"].values()) == 1]
            m["above"] = sum(1 for x in near if (x["features"] or {}).get("rsi", 0) > s["enter_rsi_high"])

    rstats = rule_stats(scored)
    ladder = stop_ladder(conn, pf.holdings, by_sym, s)
    verdicts = {}
    for x in sigs:
        d = verdicts.setdefault(x["verdict"], {"n": 0, "sum": 0})
        d["n"] += 1
        d["sum"] += x["score"]
    return {
        "day": day, "regime": regime, "tagged": tagged, "n_scored": len(scored),
        "matrix": matrix, "groups": GROUPS, "group_max": GROUP_MAX,
        "rules": rstats, "dist": distribution(scored),
        "hrules": hrules, "erules": erules, "hcount": hcount, "ecount": ecount, "missing": missing,
        "ladder": ladder, "whatif": what_if(conn, sigs, s, regime["ok"], pf.holdings) if scored else None,
        "hints": hints(sigs, ladder, rstats, missing, s, regime),
        "avg_by_verdict": {k: v["sum"] / v["n"] for k, v in verdicts.items() if v["n"]},
        "conclusion": {lst: {v: sum(1 for x in sigs if x["list"] == lst and x["verdict"] == v) for v in vs}
                       for lst, vs in (("holding", ("Sell", "Trim", "Add", "Hold")),
                                       ("watch", ("Enter", "Wait", "Avoid")), ("scan", ("Enter", "Wait", "Avoid")))},
        "labels": RULE_LABELS, "add_min": ADD_MIN_SCORE, "avoid_max": AVOID_MAX_SCORE,
    }


_ORDER = {"Sell": 0, "Trim": 1, "Add": 2, "Enter": 3, "Hold": 4, "Wait": 5, "Avoid": 6, "No data": 7}


def analysis_order(v: str) -> int:
    return _ORDER.get(v, 9)
