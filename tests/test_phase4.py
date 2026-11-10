import json
from datetime import date

from cse_analyzer import analysis, cli, notify
from cse_analyzer.trades import TradeRow, save_trades

from test_phase3 import conn, frame, uptrend  # noqa: F401  (fixture)


def test_summary_without_analysis(conn):  # noqa: F811
    s = notify.build_summary(conn)
    assert s["title"] == "CSE Analyzer: no analysis yet" and s["urgency"] == "normal"


def test_summary_lists_actions_and_is_critical_on_sell(conn):  # noqa: F811
    days = frame(uptrend()).index
    # bought DN near its top: far below a 10% stop now -> Sell
    save_trades(conn, [TradeRow(date.fromisoformat(days[0]), "DN.N0000", "BUY", 100, 50.0)], "form")
    conn.execute("INSERT INTO watchlist (symbol) VALUES ('UP.N0000')")
    conn.commit()
    analysis.run_analysis(conn)
    s = notify.build_summary(conn)
    assert s["title"].startswith("CSE: 1 Sell") and s["urgency"] == "critical"
    assert "Sell: DN" in s["body"] and f"Close of {days[-1]}" in s["body"]
    assert "ASPI uptrend" in s["body"]


def test_summary_quiet_when_nothing_to_do(conn):  # noqa: F811
    analysis.run_analysis(conn)
    s = notify.build_summary(conn)
    assert s["title"] == "CSE: nothing to act on" and s["urgency"] == "normal"


def test_summary_command(conn, tmp_path, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("CSE_DB_PATH", str(tmp_path / "t.db"))
    analysis.run_analysis(conn)
    assert cli.main(["summary"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["title"].startswith("CSE:")


def test_short_list_is_capped():
    assert notify._short([f"S{i}.N0000" for i in range(8)]) == "S0, S1, S2, S3, S4, S5 +2 more"
