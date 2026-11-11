"""Strategy settings: allowed ranges, a history of every change, named presets
and reset to defaults.

Every save keeps a snapshot, so any earlier set of values can be restored in
one click.
"""

from __future__ import annotations

import json
import sqlite3

from .db import DEFAULT_SETTINGS

# key -> (lowest, highest) allowed. Wide enough for any sane strategy, narrow
# enough to catch a typo such as a stop of 100% or a fee of 112%.
LIMITS: dict[str, tuple[float, float]] = {
    "stop_loss_pct": (1, 50),
    "risk_per_trade_pct": (0.1, 10),
    "max_weight_pct": (1, 100),
    "breakeven_at_r": (0.25, 10),
    "chandelier_atr_mult": (0.5, 10),
    "partial_take_at_r": (0.5, 20),
    "enter_rsi_low": (0, 100),
    "enter_rsi_high": (0, 100),
    "min_turnover_lkr": (0, 1_000_000_000),
    "fee_rate_pct": (0, 5),
}

# Changing any of these changes where a holding's stop sits, so stored
# trailing stops are worked out again from the new values.
STOP_KEYS = {"stop_loss_pct", "breakeven_at_r", "chandelier_atr_mult", "fee_rate_pct"}

HISTORY_LIMIT = 200


def fmt(v: float) -> str:
    return f"{v:g}" if abs(v) < 1e6 else f"{v:.0f}"


def nice(v: float) -> str:
    """For display: thousands separators on big numbers."""
    return f"{v:,.0f}" if abs(v) >= 1000 else fmt(v)


def current(conn: sqlite3.Connection) -> dict[str, str]:
    return {k: v for k, v in conn.execute("SELECT key, value FROM settings") if k in DEFAULT_SETTINGS}


def validate(raw: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (clean values, errors by key). Values are saved only when
    there are no errors."""
    values, errors = {}, {}
    for key in DEFAULT_SETTINGS:
        text = str(raw.get(key, "")).strip().replace(",", "")
        try:
            v = float(text)
        except ValueError:
            errors[key] = f"“{text}” is not a number" if text else "Required"
            continue
        lo, hi = LIMITS[key]
        if not lo <= v <= hi:
            errors[key] = f"Must be between {nice(lo)} and {nice(hi)}"
            continue
        values[key] = fmt(v)
    if "enter_rsi_low" in values and "enter_rsi_high" in values and \
            float(values["enter_rsi_low"]) >= float(values["enter_rsi_high"]):
        errors["enter_rsi_high"] = "Must be higher than the start of the RSI zone"
    if "breakeven_at_r" in values and "partial_take_at_r" in values and \
            float(values["partial_take_at_r"]) < float(values["breakeven_at_r"]):
        errors["partial_take_at_r"] = "Should not come before the break-even step"
    return values, errors


def _snapshot(conn, values: dict[str, str], note: str) -> None:
    conn.execute("INSERT INTO settings_history (values_json, note) VALUES (?, ?)",
                 (json.dumps(values, sort_keys=True), note))
    conn.execute("""DELETE FROM settings_history WHERE id NOT IN
                    (SELECT id FROM settings_history ORDER BY id DESC LIMIT ?)""", (HISTORY_LIMIT,))


def save(conn, values: dict[str, str], note: str) -> list[str]:
    """Write the values, keep a snapshot, and return the keys that changed.
    Recomputes stored trailing stops when a stop setting changed. The caller
    re-runs the analysis."""
    before = current(conn)
    if not conn.execute("SELECT 1 FROM settings_history LIMIT 1").fetchone():
        _snapshot(conn, before, "Settings before the first change")
    changed = [k for k in DEFAULT_SETTINGS if k in values and float(values[k]) != float(before.get(k, "nan"))]
    if not changed:
        return []
    for k in changed:
        conn.execute("UPDATE settings SET value = ? WHERE key = ?", (values[k], k))
    if STOP_KEYS & set(changed):
        conn.execute("UPDATE position_settings SET stop_price = NULL")
    _snapshot(conn, current(conn), note)
    conn.commit()
    return changed


def history(conn, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT * FROM settings_history ORDER BY id DESC LIMIT ?", (limit + 1,)).fetchall()
    out = []
    for i, r in enumerate(rows[:limit]):
        vals = json.loads(r["values_json"])
        prev = json.loads(rows[i + 1]["values_json"]) if i + 1 < len(rows) else None
        diff = [(k, prev.get(k), v) for k, v in vals.items() if prev and prev.get(k) != v]
        out.append({"id": r["id"], "saved_at": r["saved_at"], "note": r["note"], "values": vals, "diff": diff})
    return out


def snapshot_values(conn, snap_id: int) -> dict[str, str] | None:
    r = conn.execute("SELECT values_json FROM settings_history WHERE id = ?", (snap_id,)).fetchone()
    return json.loads(r[0]) if r else None


def presets(conn) -> list[dict]:
    return [{"name": r["name"], "saved_at": r["saved_at"], "values": json.loads(r["values_json"])}
            for r in conn.execute("SELECT * FROM settings_presets ORDER BY name COLLATE NOCASE")]


def save_preset(conn, name: str) -> None:
    conn.execute("""INSERT INTO settings_presets (name, values_json) VALUES (?, ?)
                    ON CONFLICT(name) DO UPDATE SET values_json = excluded.values_json, saved_at = datetime('now')""",
                 (name, json.dumps(current(conn), sort_keys=True)))
    conn.commit()


def preset_values(conn, name: str) -> dict[str, str] | None:
    r = conn.execute("SELECT values_json FROM settings_presets WHERE name = ?", (name,)).fetchone()
    return json.loads(r[0]) if r else None


def delete_preset(conn, name: str) -> None:
    conn.execute("DELETE FROM settings_presets WHERE name = ?", (name,))
    conn.commit()


def restore(conn, values: dict[str, str], note: str) -> tuple[list[str], dict[str, str]]:
    """Apply saved values (missing keys fall back to the default). Values are
    validated again in case limits changed since they were saved."""
    clean, errors = validate(DEFAULT_SETTINGS | values)
    if errors:
        return [], errors
    return save(conn, clean, note), {}
