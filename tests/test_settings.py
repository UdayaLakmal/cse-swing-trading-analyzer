import json

from fastapi.testclient import TestClient

from cse_analyzer import db, settings_store as st

DEFAULTS = dict(db.DEFAULT_SETTINGS)


def test_validate_ranges_and_cross_checks():
    v, e = st.validate(DEFAULTS | {"min_turnover_lkr": "2,000,000"})
    assert not e and v["min_turnover_lkr"] == "2000000"
    _, e = st.validate(DEFAULTS | {"stop_loss_pct": "100", "fee_rate_pct": "x", "enter_rsi_low": "65"})
    assert e["stop_loss_pct"] == "Must be between 1 and 50"
    assert "not a number" in e["fee_rate_pct"]
    assert "higher than" in e["enter_rsi_high"]
    _, e = st.validate(DEFAULTS | {"partial_take_at_r": "0.5"})
    assert "partial_take_at_r" in e
    assert set(st.LIMITS) == set(db.DEFAULT_SETTINGS)


def test_save_keeps_history_and_resets_stops(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO position_settings (symbol, stop_price, stop_entry_day) VALUES ('A.N0000', 9.5, '2026-01-01')")
    assert st.save(conn, DEFAULTS, "same") == []  # nothing changed, nothing logged
    assert st.save(conn, DEFAULTS | {"enter_rsi_high": "65"}, "rsi") == ["enter_rsi_high"]
    assert conn.execute("SELECT stop_price FROM position_settings").fetchone()[0] == 9.5  # not a stop setting
    assert st.save(conn, DEFAULTS | {"enter_rsi_high": "65", "chandelier_atr_mult": "2"}, "trail") == ["chandelier_atr_mult"]
    assert conn.execute("SELECT stop_price FROM position_settings").fetchone()[0] is None
    h = st.history(conn)
    assert [x["note"] for x in h] == ["trail", "rsi", "Settings before the first change"]
    assert h[0]["diff"] == [("chandelier_atr_mult", "3", "2")]
    changed, _ = st.restore(conn, st.snapshot_values(conn, h[-1]["id"]), "back")
    assert set(changed) == {"enter_rsi_high", "chandelier_atr_mult"} and st.current(conn) == DEFAULTS


def test_settings_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("CSE_DB_PATH", str(tmp_path / "t.db"))
    from cse_analyzer.web import app

    c = TestClient(app)
    conn = db.connect(tmp_path / "t.db")
    page = c.get("/settings").text
    assert "Reset to defaults" in page and "No changes yet" in page

    bad = c.post("/settings", data=DEFAULTS | {"stop_loss_pct": "150"})
    assert bad.status_code == 400 and "Nothing was saved" in bad.text and 'value="150"' in bad.text
    assert st.current(conn)["stop_loss_pct"] == "10"

    ok = c.post("/settings", data=DEFAULTS | {"stop_loss_pct": "15"}).text
    assert "changed default stop-loss" in ok and "Trailing stops were worked out again" in ok
    assert "default 10" in ok  # field flagged as different from the default

    assert "saved as “Wide”" in c.post("/settings/presets", data={"name": "  Wide "}).text
    assert "Reset to defaults: changed default stop-loss" in c.post("/settings/reset").text
    assert st.current(conn)["stop_loss_pct"] == "10"
    assert "Loaded “Wide”" in c.post("/settings/presets/load", data={"name": "Wide"}).text
    assert st.current(conn)["stop_loss_pct"] == "15"

    first = st.history(conn)[-1]["id"]
    assert f"Restored version #{first}" in c.post(f"/settings/restore/{first}").text
    assert st.current(conn) == DEFAULTS
    assert c.post("/settings/restore/9999").status_code == 404

    exp = c.get("/settings/export.json")
    assert "attachment" in exp.headers["content-disposition"]
    assert json.loads(exp.text)["presets"][0]["name"] == "Wide"
    assert "Deleted preset" in c.post("/settings/presets/delete", data={"name": "Wide"}).text
    assert st.presets(conn) == []
