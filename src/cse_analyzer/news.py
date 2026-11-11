"""Daily news headlines matched to stocks.

Two kinds of source:

* business RSS feeds (EconomyNext, Daily Mirror Business), matched against
  every listed company's name;
* a Google News search per holding and watchlist stock.

Matching is by company name, so expect some noise. Headlines are context
only: they never change a verdict.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from calendar import timegm
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import feedparser
import httpx

from .config import COLOMBO

log = logging.getLogger(__name__)

FEEDS = {
    "economynext": "https://economynext.com/feed/",
    "dailymirror": "https://www.dailymirror.lk/rss/business/215",
}
GOOGLE = "https://news.google.com/rss/search?q={q}&hl=en-LK&gl=LK&ceid=LK:en"
KEEP_DAYS = 120
MAX_AGE_DAYS = 45

# Words that say nothing about which company is meant, dropped from the end
# of a name to get the short form people write ("John Keells Holdings PLC"
# -> "John Keells Holdings" -> "John Keells").
SUFFIXES = {"PLC", "LIMITED", "LTD", "COMPANY", "CO", "HOLDINGS", "HOLDING", "GROUP", "CORPORATION",
            "CORP", "INVESTMENTS", "INVESTMENT", "THE", "AND", "OF", "SRI", "LANKA", "CEYLON", "INC"}
# A short form made only of these is too vague to match on.
VAGUE = SUFFIXES | {"BANK", "FINANCE", "CAPITAL", "INSURANCE", "LIFE", "GENERAL", "NATIONAL", "UNION",
                    "ASIA", "ASIAN", "COLOMBO", "LANKAN", "TEA", "PLANTATIONS", "HOTELS", "HOTEL",
                    "PROPERTIES", "PROPERTY", "INDUSTRIES", "MOTORS", "LEASING", "TRUST", "COMMERCIAL",
                    "DEVELOPMENT", "ENTERPRISES", "SERVICES", "INTERNATIONAL", "GLOBAL", "PEOPLE", "PEOPLES",
                    "ESTATES", "ROYAL", "CITY", "PEARL", "PRIME", "FIRST", "UNITED", "EAST", "WEST",
                    "MERCHANT", "DEVELOPMENTS", "TECHNOLOGIES", "SOLUTIONS", "FOODS", "BEVERAGE"}
# Listing pages and quote widgets, not news.
NOISE = re.compile(r"stock price|share price|seasonal returns|estimates|quote|stock forecast|dividend history"
                   r"|price target|chart|financials? \(|annual report|stock analysis|\bvaluation\b|overview",
                   re.I)
NOISE_SOURCES = {"yahoo finance", "yahoo finance uk", "tradingview", "marketscreener", "investing.com",
                 "simply wall st", "stockanalysis", "macrotrends", "wsj", "morningstar", "marketwatch",
                 "globaldata", "craft.co", "pitchbook", "zoominfo", "magzter"}


def aliases(name: str) -> list[str]:
    """Upper-case forms of a company name to look for, longest first."""
    words = re.sub(r"[^A-Z0-9&]+", " ", name.upper()).split()
    words = [w for w in words if w != "PLC"]
    out = []
    if words:
        out.append(" ".join(words))
    short = list(words)
    while short and short[-1] in SUFFIXES:
        short.pop()
    while len(short) > 1 and short[-1] in SUFFIXES | {"&"}:
        short.pop()
    if short and " ".join(short) not in out:
        out.append(" ".join(short))
    good = []
    for i, a in enumerate(out):
        ws = a.split()
        # The full name is specific once it has two words; a short form must
        # also contain a word that is not generic.
        if i == 0 and len(ws) >= 2:
            good.append(a)
        elif not all(w in VAGUE or w == "&" for w in ws) and (len(ws) > 1 or len(ws[0]) >= 4):
            good.append(a)
    return good


class Matcher:
    """Finds which companies a headline is about. When aliases overlap (John
    Keells vs John Keells Hotels), the longest one wins that stretch of text."""

    def __init__(self, stocks: list[tuple[str, str]]):
        pairs: dict[str, set[str]] = {}
        for sym, name in stocks:
            for a in aliases(name):
                pairs.setdefault(a, set()).add(sym)
        # An alias shared by several listed companies (e.g. a group name) is
        # ambiguous unless it is someone's full name.
        full = {aliases(n)[0] for _, n in stocks if aliases(n)}
        self.items = sorted(((a, syms) for a, syms in pairs.items() if len(syms) == 1 or a in full),
                            key=lambda x: -len(x[0]))
        self.patterns = [(re.compile(r"(?<![A-Z0-9])" + re.escape(a).replace(r"\ ", r"\s+") + r"(?![A-Z0-9])"), syms)
                         for a, syms in self.items]

    def match(self, text: str) -> set[str]:
        up = text.upper().replace("’", "'")
        taken: list[tuple[int, int]] = []
        found: set[str] = set()
        for pat, syms in self.patterns:
            for m in pat.finditer(up):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                taken.append((m.start(), m.end()))
                found |= syms
        return found


def _published(entry) -> str | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime.fromtimestamp(timegm(t), tz=COLOMBO).strftime("%Y-%m-%d %H:%M")


def _strip_source(title: str, source: str | None) -> str:
    """Google appends ' - Source' to every title."""
    if source and title.endswith(" - " + source):
        return title[: -len(source) - 3]
    return title


def parse_feed(content: bytes, feed: str) -> list[dict]:
    parsed = feedparser.parse(content)
    out = []
    for e in parsed.entries:
        link, title = e.get("link"), (e.get("title") or "").strip()
        if not link or not title:
            continue
        src = (e.get("source") or {}).get("title") if feed == "google" else {
            "economynext": "EconomyNext", "dailymirror": "Daily Mirror"}.get(feed, feed)
        summary = re.sub(r"<[^>]+>", " ", e.get("summary") or "")
        out.append({"link": link, "title": _strip_source(title, src), "source": src,
                    "published": _published(e), "summary": summary, "feed": feed})
    return out


def is_noise(item: dict) -> bool:
    src = (item.get("source") or "").lower()
    return bool(NOISE.search(item["title"])) or any(n in src for n in NOISE_SOURCES)


def fresh(item: dict, now: datetime) -> bool:
    if not item.get("published"):
        return True
    return item["published"] >= (now - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d")


def save(conn: sqlite3.Connection, items: list[tuple[str, dict]]) -> int:
    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO news (link, symbol, title, source, published, feed) VALUES (?, ?, ?, ?, ?, ?)""",
        [(i["link"], sym, i["title"], i.get("source"), i.get("published"), i["feed"]) for sym, i in items])
    conn.execute("DELETE FROM news WHERE COALESCE(published, fetched_at) < ?",
                 ((datetime.now(COLOMBO) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d"),))
    conn.commit()
    return conn.total_changes - before


def collect_news(conn: sqlite3.Connection, tracked: list[str], *, delay_s: float = 1.0,
                 http: httpx.Client | None = None) -> dict:
    """Fetch the business feeds and a Google News search per tracked stock."""
    stocks = [(r[0], r[1]) for r in conn.execute("SELECT symbol, name FROM stocks")]
    names = dict(stocks)
    matcher = Matcher(stocks)
    now = datetime.now(COLOMBO)
    own = http is None
    http = http or httpx.Client(timeout=20, follow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; cse-analyzer/0.1)"})
    found: list[tuple[str, dict]] = []
    stats = {"feeds": 0, "searches": 0, "failed": []}
    try:
        for feed, url in FEEDS.items():
            try:
                items = parse_feed(http.get(url).raise_for_status().content, feed)
                stats["feeds"] += 1
            except httpx.HTTPError as e:
                log.warning("news feed %s: %s", feed, e)
                stats["failed"].append(feed)
                continue
            for it in items:
                if fresh(it, now):
                    for sym in matcher.match(it["title"] + " " + it["summary"]):
                        found.append((sym, it))
        for sym in tracked:
            al = aliases(names.get(sym, ""))
            if not al:
                continue
            time.sleep(delay_s)
            q = quote_plus(f'"{al[0].title()}"')
            try:
                items = parse_feed(http.get(GOOGLE.format(q=q)).raise_for_status().content, "google")
                stats["searches"] += 1
            except httpx.HTTPError as e:
                log.warning("google news %s: %s", sym, e)
                stats["failed"].append(sym)
                continue
            base = sym.split(".")[0]
            for it in items:
                # Google also matches the body, so keep only headlines that
                # name this company (not a sister one) or its ticker.
                if not fresh(it, now) or is_noise(it):
                    continue
                if sym in matcher.match(it["title"]) or re.search(rf"\b{re.escape(base)}\b", it["title"]):
                    found.append((sym, it))
    finally:
        if own:
            http.close()
    stats["saved"] = save(conn, found)
    stats["failed"] = len(stats["failed"])
    return stats


def latest(conn, symbols: list[str] | None = None, *, days: int = 30, limit: int = 50) -> list[dict]:
    since = (datetime.now(COLOMBO) - timedelta(days=days)).strftime("%Y-%m-%d")
    q = "SELECT * FROM news WHERE COALESCE(published, fetched_at) >= ?"
    args: list = [since]
    if symbols is not None:
        if not symbols:
            return []
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += symbols
    q += " ORDER BY COALESCE(published, fetched_at) DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, [*args, limit])]
