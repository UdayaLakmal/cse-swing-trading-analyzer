"""Weekly fundamentals: dividends, net assets per share, market valuation and
company events.

cse.lk has no structured EPS or NAV figures, only report PDFs. What is read
automatically is what can be read reliably:

* dividends per share and ex-dividend dates, from each dividend announcement;
* net assets per share, from the "Net assets per share" line of the latest
  interim report (checked for sanity and always overridable by hand);
* the market's P/E, P/BV and dividend yield, from the CSE weekly report.

EPS lines are laid out too differently between companies (quarter vs
year-to-date columns, note numbers, restated figures) to read safely, so the
trailing 12-month EPS behind P/E is typed in on the Fundamentals page.

Fundamentals never change a verdict. They add context lines to it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from typing import Callable

from .config import COLOMBO
from .cse_client import CseApiError, CseClient, file_url

log = logging.getLogger(__name__)

# P/E or P/BV at or below this share of the market's counts as cheap, at or
# above RICH as expensive. Dividend yield works the other way round.
CHEAP = 0.75
RICH = 1.5
# Without market figures, fall back to plain levels.
ABS = {"pe": (8, 20), "pbv": (1.0, 3.0), "dy": 5.0}
# Re-read a report whose NAV could not be found at most this often (days).
RETRY_DAYS = 7
# A NAV from a report older than this is shown but not graded.
NAV_MAX_AGE_DAYS = 300
WEEKLY_EVERY_DAYS = 6

EVENTS = {
    # category prefix -> (label, important)
    "CASH DIVIDEND": ("Dividend", True),
    "SCRIP DIVIDEND": ("Scrip dividend", True),
    "RIGHTS ISSUE": ("Rights issue", True),
    "SUB-DIVISION": ("Share split", True),
    "SUBDIVISION": ("Share split", True),
    "CONSOLIDATION": ("Share consolidation", True),
    "BONUS": ("Bonus issue", True),
    "TRADING HALT": ("Trading halt", True),
    "TRADING SUSPEN": ("Trading suspended", True),
    "DEALINGS BY DIRECTORS": ("Director dealing", False),
    "FINANCIAL": ("Results", False),
    "MANDATORY OFFER": ("Takeover offer", True),
    "VOLUNTARY OFFER": ("Takeover offer", True),
    "DELISTING": ("Delisting", True),
}


# --- parsing ------------------------------------------------------------------

NUM = re.compile(r"\(?-?\d[\d,]*\.\d+\)?")
DATE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")
NAV_LABEL = re.compile(r"net\s+asset(?:s)?\s+(?:value\s+)?per\s+(?:ordinary\s+)?share", re.I)


def _num(tok: str) -> float:
    neg = tok.startswith("(") or tok.startswith("-")
    v = float(tok.strip("()-").replace(",", ""))
    return -v if neg else v


def parse_nav(text: str) -> float | None:
    """Net assets per share from an interim report's text (pdftotext -layout).

    Takes the first figure after the first "Net assets per share" label that
    has figures on its line (or the line below). That is the current period of
    the first statement, which is the group's when the company has one. Note
    numbers are whole numbers, so only figures with decimals count.
    """
    lines = [DATE.sub(" ", ln) for ln in text.splitlines()]
    for i, line in enumerate(lines):
        m = NAV_LABEL.search(line)
        if not m:
            continue
        nums = NUM.findall(line[m.end():])
        # Some reports put the column dates on the label line and the figures
        # a few lines below it.
        for nxt in lines[i + 1:i + 4]:
            if nums or re.search(r"[A-Za-z]{3}", nxt):
                break
            nums = NUM.findall(nxt)
        if nums and _num(nums[0]) > 0:
            return _num(nums[0])
    return None


def parse_market_ratios(text: str) -> dict[str, float]:
    """Market PER, PBV and DY from the first page of the CSE weekly report,
    where each sits on its own line followed by this week's and last week's
    values."""
    out: dict[str, float] = {}
    for line in text.splitlines()[:80]:
        m = re.search(r"\b(PER|PBV|DY)\b\s+(\d+(?:\.\d+)?)\s+\d+(?:\.\d+)?\s*$", line)
        if m and m.group(1).lower() not in out:
            out[{"PER": "per", "PBV": "pbv", "DY": "dy"}[m.group(1)]] = float(m.group(2))
    return out


def parse_cse_date(s: str | None) -> str | None:
    """'12 Nov 2026' or '03 Nov 2026 02:38:20 PM' -> '2026-11-12'."""
    if not s:
        return None
    for fmt in ("%d %b %Y", "%d %b %Y %I:%M:%S %p", "%d/%b/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_dividend(detail: dict) -> dict | None:
    """One cash dividend from getAnnouncementById."""
    aid, base = detail.get("id"), detail.get("symbol")
    if aid is None or not base:
        return None
    voting = detail.get("votingDivPerShare")
    if voting is None:
        voting = detail.get("divPerShare")
    return {
        "announcement_id": int(aid),
        "base": base.split(".")[0].strip().upper(),
        "voting_dps": voting,
        "non_voting_dps": detail.get("nonVotingDivPerShare"),
        "xd": parse_cse_date(detail.get("xd")),
        "payment": parse_cse_date(detail.get("payment")),
        "announced": parse_cse_date(detail.get("dateOfAnnouncement")),
        "remarks": (detail.get("remarks") or "").strip() or None,
    }


def pdf_text(data: bytes) -> str:
    """Text of a PDF with its layout kept, via poppler's pdftotext. Empty if
    the tool is missing or the PDF is a scan with no text layer."""
    exe = shutil.which("pdftotext")
    if not exe:
        log.warning("pdftotext is not installed: report PDFs cannot be read")
        return ""
    try:
        r = subprocess.run([exe, "-layout", "-q", "-", "-"], input=data, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.decode("utf-8", "replace")


def event_label(category: str | None) -> tuple[str, bool]:
    c = (category or "").upper().strip()
    for prefix, v in EVENTS.items():
        if c.startswith(prefix):
            return v
    return (c.title() if c else "Announcement"), False


# --- what to fetch --------------------------------------------------------------


def tracked_symbols(conn: sqlite3.Connection) -> list[str]:
    """Holdings, the watchlist and today's market Enter ideas."""
    from .portfolio import build_portfolio

    syms = {h.symbol for h in build_portfolio(conn).holdings}
    syms |= {r[0] for r in conn.execute("SELECT symbol FROM watchlist")}
    day = conn.execute("SELECT MAX(day) FROM signals").fetchone()[0]
    if day:
        syms |= {r[0] for r in conn.execute("SELECT symbol FROM signals WHERE day = ? AND verdict = 'Enter'", (day,))}
    known = {r[0] for r in conn.execute("SELECT symbol FROM stocks")}
    return sorted(s for s in syms if s in known)


# --- storage ----------------------------------------------------------------------


def save_dividend(conn, d: dict) -> None:
    conn.execute(
        """INSERT INTO dividends (announcement_id, base, voting_dps, non_voting_dps, xd, payment, announced, remarks)
           VALUES (:announcement_id, :base, :voting_dps, :non_voting_dps, :xd, :payment, :announced, :remarks)
           ON CONFLICT(announcement_id) DO UPDATE SET voting_dps = excluded.voting_dps,
             non_voting_dps = excluded.non_voting_dps, xd = excluded.xd, payment = excluded.payment,
             remarks = excluded.remarks""", d)


def fetch_new_dividends(conn, client: CseClient, limit: int = 60) -> int:
    """Details for stored dividend announcements that have no amount yet."""
    rows = conn.execute(
        """SELECT raw_json FROM announcements WHERE kind = 'approved' AND category = 'CASH DIVIDEND'
           ORDER BY id DESC""").fetchall()
    have = {r[0] for r in conn.execute("SELECT announcement_id FROM dividends")}
    n = 0
    for (raw,) in rows:
        aid = json.loads(raw).get("announcementId")
        if aid is None or aid in have or n >= limit:
            continue
        have.add(aid)
        try:
            d = parse_dividend(client.announcement_detail(aid))
        except CseApiError as e:
            log.warning("dividend %s: %s", aid, e)
            continue
        if d:
            save_dividend(conn, d)
            n += 1
    conn.commit()
    return n


def latest_interim(fin: dict) -> dict | None:
    items = [x for x in fin.get("infoQuarterlyData") or [] if x.get("path")]
    # Sort by upload time: the typed-in report date is sometimes wrong (a
    # 2020 report dated 2030 was seen).
    items.sort(key=lambda x: x.get("uploadedDate") or x.get("manualDate") or 0, reverse=True)
    return items[0] if items else None


def report_day(rep: dict) -> str | None:
    """The period-end date of a report, but never later than its upload."""
    stamps = [v for v in (rep.get("manualDate"), rep.get("uploadedDate")) if isinstance(v, (int, float))]
    return datetime.fromtimestamp(min(stamps) / 1000, tz=COLOMBO).date().isoformat() if stamps else None


def refresh_nav(conn, client: CseClient, symbol: str, *, force: bool = False) -> str:
    """Read NAV per share from the latest interim report if it is new.
    Returns 'new', 'same', 'not found' or 'no report'."""
    rep = latest_interim(client.financials(symbol))
    if not rep:
        return "no report"
    url = file_url(rep["path"])
    row = conn.execute("SELECT nav_source, nav_auto, nav_checked FROM fundamentals WHERE symbol = ?", (symbol,)).fetchone()
    if row and row["nav_source"] == url and not force:
        recent = row["nav_checked"] and row["nav_checked"] >= (date.today() - timedelta(days=RETRY_DAYS)).isoformat()
        if row["nav_auto"] is not None or recent:
            return "same"
    nav = parse_nav(pdf_text(client.download(url)))
    rep_day = report_day(rep)
    conn.execute(
        """INSERT INTO fundamentals (symbol, nav_auto, nav_source, nav_period, nav_report_day, nav_checked)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET nav_auto = excluded.nav_auto, nav_source = excluded.nav_source,
             nav_period = excluded.nav_period, nav_report_day = excluded.nav_report_day,
             nav_checked = excluded.nav_checked""",
        (symbol, nav, url, (rep.get("fileText") or "").strip(), rep_day, date.today().isoformat()))
    conn.commit()
    return "new" if nav is not None else "not found"


def refresh_market(conn, client: CseClient, *, force: bool = False) -> dict | None:
    reports = [r for r in client.weekly_reports() if r.get("path")]
    if not reports:
        return None
    rep = max(reports, key=lambda r: r.get("manual_date") or r.get("uploaded_date") or 0)
    ms = rep.get("manual_date") or rep.get("uploaded_date")
    day = datetime.fromtimestamp(ms / 1000, tz=COLOMBO).date().isoformat() if ms else date.today().isoformat()
    if not force and conn.execute("SELECT 1 FROM market_valuation WHERE report_day = ?", (day,)).fetchone():
        return None
    url = file_url(rep["path"])
    ratios = parse_market_ratios(pdf_text(client.download(url)))
    if not ratios:
        log.warning("weekly report %s: market PER/PBV/DY not found", url)
        return None
    conn.execute(
        """INSERT INTO market_valuation (report_day, per, pbv, dy, source) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(report_day) DO UPDATE SET per = excluded.per, pbv = excluded.pbv, dy = excluded.dy,
             source = excluded.source""",
        (day, ratios.get("per"), ratios.get("pbv"), ratios.get("dy"), url))
    conn.commit()
    return ratios | {"report_day": day}


def collect_weekly(conn, client: CseClient, *, force: bool = False,
                   progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Announcements, dividends and NAV for tracked stocks, and market ratios."""
    from .collector import _finish_run, _start_run, upsert_announcements

    today = date.today()
    run_id = _start_run(conn, "weekly", today)
    stats: dict = {"stocks": 0, "announcements": 0, "dividends": 0, "nav_new": 0, "nav_not_found": [],
                   "market": None, "failed": []}
    try:
        try:
            stats["market"] = refresh_market(conn, client, force=force)
        except CseApiError as e:
            log.warning("weekly report: %s", e)
        syms = tracked_symbols(conn)
        stats["stocks"] = len(syms)
        start = (today - timedelta(days=400)).isoformat()
        for i, sym in enumerate(syms, 1):
            try:
                items = client.company_announcements(sym, start, today.isoformat())
                stats["announcements"] += upsert_announcements(conn, "approved", items, base=sym.split(".")[0])
                conn.commit()
                res = refresh_nav(conn, client, sym, force=force)
                if res == "new":
                    stats["nav_new"] += 1
                elif res == "not found":
                    stats["nav_not_found"].append(sym)
            except CseApiError as e:
                log.warning("%s: %s", sym, e)
                stats["failed"].append(sym)
            progress(f"{i}/{len(syms)} {sym}")
        stats["dividends"] = fetch_new_dividends(conn, client, limit=200)
    except Exception as e:
        _finish_run(conn, run_id, "failed", str(e))
        raise
    failed = len(stats["failed"])
    status = "ok" if not failed else ("partial" if failed < max(stats["stocks"], 1) else "failed")
    _finish_run(conn, run_id, status, json.dumps({k: (len(v) if isinstance(v, list) else v)
                                                  for k, v in stats.items() if k != "market"}))
    stats["status"] = status
    return stats


def weekly_due(conn) -> bool:
    row = conn.execute(
        "SELECT MAX(run_day) FROM runs WHERE kind = 'weekly' AND status IN ('ok', 'partial')").fetchone()
    return not row[0] or row[0] <= (date.today() - timedelta(days=WEEKLY_EVERY_DAYS)).isoformat()


# --- valuation ------------------------------------------------------------------------


def market(conn) -> dict | None:
    r = conn.execute("SELECT * FROM market_valuation ORDER BY report_day DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _dps_key(symbol: str) -> str:
    return "non_voting_dps" if ".X" in symbol else "voting_dps"


def grade(pe: float | None, pbv: float | None, dy: float | None, mkt: dict | None) -> tuple[str, list[tuple[str, int]]]:
    """Valuation grade from transparent points, each worth +1 (cheap) or -1."""
    pts: list[tuple[str, int]] = []
    mkt = mkt or {}

    def rel(name: str, v: float | None, m: float | None, lo: float, hi: float, label: str) -> None:
        if v is None:
            return
        if m:
            if v <= CHEAP * m:
                pts.append((f"{label} {v:.1f} is well below the market's {m:.1f}", +1))
            elif v >= RICH * m:
                pts.append((f"{label} {v:.1f} is well above the market's {m:.1f}", -1))
            else:
                pts.append((f"{label} {v:.1f} is close to the market's {m:.1f}", 0))
        elif v <= lo:
            pts.append((f"{label} {v:.1f} is low", +1))
        elif v >= hi:
            pts.append((f"{label} {v:.1f} is high", -1))
        else:
            pts.append((f"{label} {v:.1f} is moderate", 0))

    if pe is not None and pe <= 0:
        pts.append(("Loss-making over the last 12 months (EPS below zero)", -1))
    else:
        rel("pe", pe, mkt.get("per"), *ABS["pe"], "P/E")
    rel("pbv", pbv, mkt.get("pbv"), *ABS["pbv"], "P/BV")
    if dy is not None:
        m = mkt.get("dy")
        good = dy >= (1.25 * m if m else ABS["dy"])
        if dy == 0:
            pts.append(("No cash dividend in the last 12 months", 0))
        else:
            pts.append((f"Dividend yield {dy:.1f}% " + ("beats" if good else "is at or below")
                        + (f" the market's {m:.1f}%" if m else f" {ABS['dy']:g}%"), +1 if good else 0))
    if not pts:
        return "Unknown", pts
    total = sum(p for _, p in pts)
    label = ("Cheap" if total >= 2 else "Leans cheap" if total == 1 else "Fair" if total == 0
             else "Leans expensive" if total == -1 else "Expensive")
    return label, pts


def valuations(conn, closes: dict[str, float]) -> dict[str, dict]:
    """Valuation for each symbol given its latest close."""
    mkt = market(conn)
    fund = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM fundamentals")}
    today = date.today().isoformat()
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    nav_cutoff = (date.today() - timedelta(days=NAV_MAX_AGE_DAYS)).isoformat()
    divs: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM dividends ORDER BY COALESCE(xd, announced) DESC"):
        divs.setdefault(r["base"], []).append(dict(r))
    # Until a weekly run has looked, no dividend rows does not mean no dividends.
    weekly_done = conn.execute(
        "SELECT 1 FROM runs WHERE kind = 'weekly' AND status IN ('ok', 'partial') LIMIT 1").fetchone() is not None
    out = {}
    for sym, close in closes.items():
        f = fund.get(sym, {})
        base = sym.split(".")[0]
        key = _dps_key(sym)
        mine = [d for d in divs.get(base, []) if d.get(key)]
        paid = [d for d in mine if year_ago < (d["xd"] or d["announced"] or "") <= today]
        upcoming = sorted((d for d in mine if d["xd"] and d["xd"] > today), key=lambda d: d["xd"])
        dps = sum(d[key] for d in paid)
        nav = f.get("nav_manual") or f.get("nav_auto")
        eps = f.get("eps_ttm")
        pbv = close / nav if nav and close else None
        # A loss (EPS at or below zero) is graded as P/E 0, shown as "loss".
        pe = None if eps is None or not close else (close / eps if eps > 0 else 0.0)
        old_report = bool(f.get("nav_report_day") and f["nav_report_day"] < nav_cutoff)
        suspect = pbv is not None and not f.get("nav_manual") and (old_report or not 0.02 <= pbv <= 50)
        dy = (100 * dps / close) if close and (divs.get(base) or weekly_done) else None
        label, pts = grade(pe, None if suspect else pbv, dy, mkt)
        out[sym] = {
            "close": close, "nav": nav, "nav_from": "manual" if f.get("nav_manual") else ("auto" if f.get("nav_auto") else None),
            "nav_auto": f.get("nav_auto"), "nav_manual": f.get("nav_manual"), "nav_source": f.get("nav_source"),
            "nav_period": f.get("nav_period"), "nav_report_day": f.get("nav_report_day"),
            "old_report": old_report, "nav_checked": f.get("nav_checked"), "eps": eps, "note": f.get("note"),
            "pe": pe, "loss": eps is not None and eps <= 0, "pbv": pbv, "suspect": suspect, "dps": dps, "dy": dy, "paid": paid,
            "next_div": upcoming[0] if upcoming else None, "grade": label, "points": pts, "market": mkt,
            "updated_at": f.get("updated_at"),
        }
    return out


def save_manual(conn, symbol: str, *, eps: float | None, nav: float | None, note: str | None) -> None:
    conn.execute(
        """INSERT INTO fundamentals (symbol, eps_ttm, nav_manual, note, updated_at) VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
           ON CONFLICT(symbol) DO UPDATE SET eps_ttm = excluded.eps_ttm, nav_manual = excluded.nav_manual,
             note = excluded.note, updated_at = excluded.updated_at""",
        (symbol, eps, nav, note))
    conn.commit()


# --- events ------------------------------------------------------------------------------


def events(conn, symbols: list[str], days: int = 120) -> dict[str, list[dict]]:
    """Recent announcements per symbol, newest first, with upcoming
    ex-dividend dates on top."""
    since = date.today() - timedelta(days=days)
    bases = {s.split(".")[0]: s for s in symbols}
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    if not bases:
        return out
    marks = ",".join("?" * len(bases))
    for r in conn.execute(f"SELECT * FROM announcements WHERE symbol IN ({marks})", list(bases)):
        day = parse_cse_date(r["announced"])
        if not day or day < since.isoformat():
            continue
        label, important = event_label(r["category"])
        title = r["title"] if r["kind"] == "financial" else None
        out[bases[r["symbol"]]].append({"day": day, "label": label, "important": important, "title": title,
                                        "url": r["url"], "kind": r["kind"]})
    today = date.today().isoformat()
    for d in conn.execute(f"SELECT * FROM dividends WHERE base IN ({marks}) AND xd >= ?", [*bases, today]):
        sym = bases[d["base"]]
        amt = d[_dps_key(sym)]
        if amt:
            out[sym].append({"day": d["xd"], "label": "Goes ex-dividend", "important": True, "upcoming": True,
                             "title": f"Rs {amt:g} per share, paid {d['payment'] or 'later'}", "url": None, "kind": "xd"})
    for sym, lst in out.items():
        # One line per label and day: CSE often files the same event twice.
        seen, uniq = set(), []
        for e in sorted(lst, key=lambda e: (not e.get("upcoming"), e["day"]), reverse=False):
            k = (e["label"], e["day"], e["title"])
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        upcoming = [e for e in uniq if e.get("upcoming")]
        past = sorted((e for e in uniq if not e.get("upcoming")), key=lambda e: e["day"], reverse=True)
        out[sym] = upcoming + past
    return out


def upcoming_xd(conn, symbols: list[str], within_days: int = 10) -> dict[str, dict]:
    """Next ex-dividend date within N days, per symbol."""
    today = date.today()
    out = {}
    for sym in symbols:
        r = conn.execute(
            "SELECT * FROM dividends WHERE base = ? AND xd >= ? AND xd <= ? ORDER BY xd LIMIT 1",
            (sym.split(".")[0], today.isoformat(), (today + timedelta(days=within_days)).isoformat())).fetchone()
        if r and r[_dps_key(sym)]:
            out[sym] = {"xd": r["xd"], "dps": r[_dps_key(sym)], "payment": r["payment"]}
    return out
