from datetime import date, timedelta

import httpx
import pytest

from cse_analyzer import analysis, collector, fundamentals as fu, news, notify
from test_phase3 import conn, frame, uptrend  # noqa: F401  (fixture)

# --- parsing -------------------------------------------------------------------

NAV_SAME_LINE = """
Total equity                                   26,654,321     25,998,000
Net assets per share                                                 23.60          23.38
Net assets per share                                                 14.16          13.94
10.3 Net assets per share
"""
NAV_DATES_ON_LABEL = """
                                                    As at          As at
2 Net Asset Value per Share (Rs.)                  30.09.2026     30.09.2025

                                                         82.48        354.96
"""
NAV_NOTE_FIRST = "19   Net Assets Per Share (Rs.)          11       (0.00)    \n Net assets per share    31.89   39.66\n"


def test_parse_nav_layouts():
    assert fu.parse_nav(NAV_SAME_LINE) == 23.60
    assert fu.parse_nav(NAV_DATES_ON_LABEL) == 82.48  # dates are not figures
    assert fu.parse_nav(NAV_NOTE_FIRST) == 31.89  # a zero/negative or whole note number is skipped
    assert fu.parse_nav("Net Assets Value Per share   (1.25)  2.00") is None  # negative NAV is not used
    assert fu.parse_nav("no figures here") is None


def test_parse_market_ratios():
    text = "\n".join([
        "   ASPI     21,000.12   20,900.00",
        "                       PER                    10.36        10.15",
        "midj PBV                                       1.20         1.17",
        "                        DY                     3.32         3.38",
        "     Industry Group   PER   PBV   DY",
    ])
    assert fu.parse_market_ratios(text) == {"per": 10.36, "pbv": 1.2, "dy": 3.32}


def test_parse_dividend_and_dates():
    d = fu.parse_dividend({"id": 39867, "symbol": "JKH", "votingDivPerShare": 0.1, "nonVotingDivPerShare": 0,
                           "xd": "12 Nov 2026", "payment": "02 Dec 2026", "dateOfAnnouncement": "03 Nov 2026",
                           "remarks": " Interim dividend "})
    assert d == {"announcement_id": 39867, "base": "JKH", "voting_dps": 0.1, "non_voting_dps": 0,
                 "xd": "2026-11-12", "payment": "2026-12-02", "announced": "2026-11-03", "remarks": "Interim dividend"}
    assert fu.parse_dividend({"id": 1}) is None
    assert fu.parse_cse_date("03 Nov 2026 02:38:20 PM") == "2026-11-03"
    assert fu.parse_cse_date("soon") is None


def test_latest_interim_ignores_bad_typed_dates():
    fin = {"infoQuarterlyData": [
        {"path": "cmt/old.pdf", "manualDate": 1917000000000, "uploadedDate": 1600000000000},  # typed as 2030
        {"path": "cmt/new.pdf", "manualDate": 1782777600000, "uploadedDate": 1785229278745},
    ]}
    rep = fu.latest_interim(fin)
    assert rep["path"] == "cmt/new.pdf"
    assert fu.report_day(fin["infoQuarterlyData"][0]) < "2021-01-01"  # never later than the upload
    from cse_analyzer.cse_client import file_url
    assert file_url("upload_report_file/x.pdf") == "https://cdn.cse.lk/cmt/upload_report_file/x.pdf"
    assert file_url("cmt/x.pdf") == "https://cdn.cse.lk/cmt/x.pdf"


def test_event_labels():
    assert fu.event_label("RIGHTS ISSUE (DATES)") == ("Rights issue", True)
    assert fu.event_label("DEALINGS BY DIRECTORS") == ("Director dealing", False)
    assert fu.event_label("CHANGE OF COMPANY ADDRESS") == ("Change Of Company Address", False)


# --- valuation -------------------------------------------------------------------


def test_grade_points():
    mkt = {"per": 10.0, "pbv": 1.2, "dy": 3.0}
    g, pts = fu.grade(6.0, 0.8, 5.0, mkt)
    assert g == "Cheap" and [p for _, p in pts] == [1, 1, 1]
    g, pts = fu.grade(20.0, 2.0, 0.0, mkt)
    assert g == "Expensive" and [p for _, p in pts] == [-1, -1, 0]
    assert fu.grade(0.0, None, None, mkt)[0] == "Leans expensive"  # a loss
    assert fu.grade(None, None, None, mkt) == ("Unknown", [])
    assert fu.grade(None, 0.5, None, None)[0] == "Leans cheap"  # absolute levels without market figures


def test_valuations_from_stored_figures(conn):  # noqa: F811
    today = date.today()
    conn.execute("INSERT INTO market_valuation VALUES ('2026-10-30', 10, 1.2, 3, 'u')")
    conn.execute("INSERT INTO fundamentals (symbol, nav_auto, nav_report_day, eps_ttm) VALUES ('UP.N0000', 20, ?, 2)",
                 (today.isoformat(),))
    conn.execute("INSERT INTO fundamentals (symbol, nav_auto, nav_report_day) VALUES ('DN.N0000', 5, '2020-06-30')")
    for aid, xd, dps in ((1, today - timedelta(days=30), 0.5), (2, today - timedelta(days=400), 9.0),
                         (3, today + timedelta(days=5), 0.25)):
        fu.save_dividend(conn, {"announcement_id": aid, "base": "UP", "voting_dps": dps, "non_voting_dps": None,
                                "xd": xd.isoformat(), "payment": None, "announced": None, "remarks": None})
    v = fu.valuations(conn, {"UP.N0000": 10.0, "DN.N0000": 10.0})
    up, dn = v["UP.N0000"], v["DN.N0000"]
    assert up["pe"] == 5.0 and up["pbv"] == 0.5 and up["dy"] == pytest.approx(5.0)  # only the last 12 months
    assert up["next_div"]["voting_dps"] == 0.25 and up["grade"] == "Cheap"
    assert dn["old_report"] and dn["suspect"] and dn["grade"] == "Unknown"  # old NAV left out, no weekly run yet
    fu.save_manual(conn, "DN.N0000", eps=-1.0, nav=10.0, note="checked")
    dn = fu.valuations(conn, {"DN.N0000": 10.0})["DN.N0000"]
    assert dn["loss"] and dn["pbv"] == 1.0 and not dn["suspect"] and dn["nav_from"] == "manual"
    assert fu.upcoming_xd(conn, ["UP.N0000", "DN.N0000"]) == {
        "UP.N0000": {"xd": (today + timedelta(days=5)).isoformat(), "dps": 0.25, "payment": None}}


def test_context_lines_do_not_change_verdicts(conn):  # noqa: F811
    from cse_analyzer.trades import TradeRow, save_trades

    days = frame(uptrend()).index
    save_trades(conn, [TradeRow(date.fromisoformat(days[100]), "UP.N0000", "BUY", 100, uptrend()[100])], "form")
    analysis.run_analysis(conn)
    before = {s["symbol"]: s["verdict"] for s in analysis.load_signals(conn)[1]}
    xd = date.today() + timedelta(days=3)
    fu.save_dividend(conn, {"announcement_id": 7, "base": "UP", "voting_dps": 0.4, "non_voting_dps": None,
                            "xd": xd.isoformat(), "payment": None, "announced": None, "remarks": None})
    conn.execute("""INSERT INTO announcements (id, kind, company, symbol, category, title, announced, raw_json)
                    VALUES (1, 'approved', 'UP PLC', 'UP', 'RIGHTS ISSUE', 'RIGHTS ISSUE', ?, '{}')""",
                 (date.today().strftime("%d %b %Y"),))
    fu.save_manual(conn, "UP.N0000", eps=1.0, nav=None, note=None)
    conn.commit()
    analysis.run_analysis(conn)
    sigs = {s["symbol"]: s for s in analysis.load_signals(conn)[1]}
    assert {k: v["verdict"] for k, v in sigs.items()} == before
    ctx = sigs["UP.N0000"]["context"]
    assert any(c.startswith("Valuation:") for c in ctx)
    assert any("Goes ex-dividend on " + xd.isoformat() in c and "tight stop" in c for c in ctx)
    assert any(c.startswith("Rights issue announced") for c in ctx)
    assert "Ex-dividend soon: UP" in notify.build_summary(conn)["body"]


# --- collection ---------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.downloads = []

    def weekly_reports(self):
        return [{"path": "upload_report_file/w.pdf", "manual_date": 1793358941000}]

    def company_announcements(self, symbol, start, end):
        return [{"id": 900, "announcementId": 39867, "announcementCategory": "CASH DIVIDEND", "company": "UP PLC",
                 "dateOfAnnouncement": "03 Nov 2026"}]

    def announcement_detail(self, aid):
        return {"id": aid, "symbol": "UP", "votingDivPerShare": 0.3, "xd": "12 Nov 2026"}

    def financials(self, symbol):
        return {"infoQuarterlyData": [{"path": f"cmt/{symbol}.pdf", "fileText": "Interim Q2",
                                       "manualDate": 1790706600000, "uploadedDate": 1793696900421}]}

    def download(self, url):
        self.downloads.append(url)
        return url.encode()


def fake_pdf_text(data: bytes) -> str:
    if data.endswith(b"w.pdf"):
        return "PER   10.36   10.15\nPBV   1.20   1.17\nDY   3.32   3.38\n"
    return "Net assets per share    12.50    11.00\n"


def test_collect_weekly(conn, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fu, "pdf_text", fake_pdf_text)
    conn.execute("INSERT INTO watchlist (symbol) VALUES ('UP.N0000')")
    conn.commit()
    assert fu.weekly_due(conn)
    client = FakeClient()
    st = fu.collect_weekly(conn, client)
    assert st["status"] == "ok" and st["stocks"] == 1 and st["nav_new"] == 1 and st["dividends"] == 1
    assert st["market"]["per"] == 10.36
    row = conn.execute("SELECT * FROM fundamentals WHERE symbol = 'UP.N0000'").fetchone()
    assert row["nav_auto"] == 12.5 and row["nav_report_day"] == "2026-09-30"
    assert conn.execute("SELECT symbol FROM announcements WHERE id = 900").fetchone()[0] == "UP"
    assert not fu.weekly_due(conn)
    # A second run does not download the same report again.
    n = len(client.downloads)
    fu.collect_weekly(conn, client)
    assert len(client.downloads) == n


def test_approved_announcements_get_symbol_by_name(conn):  # noqa: F811
    collector.upsert_announcements(conn, "approved", [
        {"id": 5, "announcementCategory": "CASH DIVIDEND", "company": "UP  PLC", "dateOfAnnouncement": "03 Nov 2026"},
        {"id": 6, "announcementCategory": "CASH DIVIDEND", "company": "SOMEONE ELSE PLC"}])
    got = dict(conn.execute("SELECT id, symbol FROM announcements"))
    assert got == {5: "UP", 6: None}


# --- news ------------------------------------------------------------------------------

STOCKS = [("JKH.N0000", "JOHN KEELLS HOLDINGS PLC"), ("KHL.N0000", "JOHN KEELLS HOTELS PLC"),
          ("COMB.N0000", "COMMERCIAL BANK OF CEYLON PLC"), ("SINS.N0000", "SINGER (SRI LANKA) PLC"),
          ("HAYL.N0000", "HAYLEYS PLC"), ("DIPD.N0000", "DIPPED PRODUCTS PLC")]


def test_aliases_and_matching():
    assert news.aliases("JOHN KEELLS HOLDINGS PLC") == ["JOHN KEELLS HOLDINGS", "JOHN KEELLS"]
    assert news.aliases("COMMERCIAL BANK OF CEYLON PLC") == ["COMMERCIAL BANK OF CEYLON"]  # "Commercial Bank" is too vague
    m = news.Matcher(STOCKS)
    assert m.match("John Keells Hotels revenue rises") == {"KHL.N0000"}
    assert m.match("John Keells Holdings posts profit") == {"JKH.N0000"}
    assert m.match("John Keells group to invest") == {"JKH.N0000"}
    assert m.match("Singer’s quarterly profit falls") == {"SINS.N0000"}
    assert m.match("Commercial Bank of Ceylon and Hayleys sign MoU") == {"COMB.N0000", "HAYL.N0000"}
    assert m.match("Sri Lanka tea prices rise") == set()


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Hayleys profit up 20pct</title><link>https://x/1</link><pubDate>{d}</pubDate><description>Hayleys PLC said</description></item>
<item><title>Rupee steady</title><link>https://x/2</link><pubDate>{d}</pubDate></item>
<item><title>Old Hayleys story</title><link>https://x/3</link><pubDate>Mon, 01 Jan 2024 08:00:00 GMT</pubDate></item>
</channel></rss>"""
GNEWS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>
<item><title>Dipped Products buys stake - EconomyNext</title><link>https://g/1</link><pubDate>{d}</pubDate><source url="https://economynext.com">EconomyNext</source></item>
<item><title>Dipped Products PLC (DIPD) stock price - Yahoo Finance</title><link>https://g/2</link><pubDate>{d}</pubDate><source url="https://yahoo.com">Yahoo Finance</source></item>
<item><title>Colombo stocks close higher - Daily FT</title><link>https://g/3</link><pubDate>{d}</pubDate><source url="https://ft.lk">Daily FT</source></item>
</channel></rss>"""


def test_collect_news(tmp_path):
    from cse_analyzer import db

    c = db.connect(tmp_path / "n.db")
    c.executemany("INSERT INTO stocks VALUES (?, ?, ?, NULL, '')", [(i, s, n) for i, (s, n) in enumerate(STOCKS, 1)])
    d = date.today().strftime("%a, %d %b %Y 08:00:00 GMT").encode()

    def handler(req: httpx.Request) -> httpx.Response:
        body = GNEWS if req.url.host == "news.google.com" else RSS
        return httpx.Response(200, content=body.replace(b"{d}", d))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        st = news.collect_news(c, ["DIPD.N0000"], delay_s=0, http=http)
    assert st["feeds"] == 2 and st["searches"] == 1 and st["failed"] == 0
    rows = {(r["symbol"], r["title"], r["source"]) for r in news.latest(c)}
    # Both mocked feeds carry the same link: stored once per stock. The quote
    # page and the headline that does not name the company are dropped.
    assert rows == {("HAYL.N0000", "Hayleys profit up 20pct", "EconomyNext"),
                    ("DIPD.N0000", "Dipped Products buys stake", "EconomyNext")}
    assert news.latest(c, []) == []


# --- pages -----------------------------------------------------------------------------


def test_fundamentals_pages(conn, tmp_path, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CSE_DB_PATH", str(tmp_path / "t.db"))
    from cse_analyzer import web

    c = TestClient(web.app)
    conn.execute("INSERT INTO watchlist (symbol) VALUES ('UP.N0000')")
    conn.commit()
    c.post("/analysis/run")
    page = c.get("/fundamentals").text
    assert "Fundamentals &amp; news" in page and "UP</a>" in page and "Not read yet" in page
    r = c.post("/fundamentals/UP.N0000", data={"eps": "2", "nav": "", "note": "from Q2 report"})
    assert "saved" in r.text
    assert conn.execute("SELECT eps_ttm, note FROM fundamentals WHERE symbol = 'UP.N0000'").fetchone()[:] == (2.0, "from Q2 report")
    assert "must be a number" in c.post("/fundamentals/UP.N0000", data={"eps": "abc"}).text
    assert "above zero" in c.post("/fundamentals/UP.N0000", data={"eps": "1", "nav": "-3"}).text
    assert c.post("/fundamentals/NOPE.N0000", data={"eps": "1"}).status_code == 404
    stock = c.get("/stock/UP.N0000").text
    assert "Fundamentals" in stock and "EPS 2.00 (yours)" in stock

    # "refresh" is its own route, not a symbol
    monkeypatch.setattr(web.fundamentals, "collect_weekly", lambda *a, **k: {"stocks": 1, "nav_new": 0, "dividends": 0})
    monkeypatch.setattr(web.news, "collect_news", lambda *a, **k: {"saved": 3})
    assert "Refreshed 1 stocks" in c.post("/fundamentals/refresh").text
    assert "News &amp; events" in c.get("/").text
