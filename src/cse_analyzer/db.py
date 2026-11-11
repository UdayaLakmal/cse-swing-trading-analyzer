"""SQLite storage. One file in a mounted volume; schema is created on connect."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    stock_id    INTEGER PRIMARY KEY,          -- cse.lk internal id
    symbol      TEXT NOT NULL UNIQUE,         -- e.g. JKH.N0000
    name        TEXT NOT NULL,
    market_cap  REAL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices_daily (
    stock_id  INTEGER NOT NULL REFERENCES stocks(stock_id),
    day       TEXT NOT NULL,                  -- YYYY-MM-DD, Colombo
    high      REAL,
    low       REAL,
    close     REAL,
    volume    INTEGER,
    PRIMARY KEY (stock_id, day)
);

CREATE TABLE IF NOT EXISTS index_daily (
    index_code  TEXT NOT NULL,                -- ASPI, SNPSL20
    day         TEXT NOT NULL,
    value       REAL NOT NULL,
    change_pct  REAL,
    PRIMARY KEY (index_code, day)
);

-- Trades are the source of truth; holdings and P/L are derived from them.
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    price       REAL NOT NULL CHECK (price > 0),
    fees        REAL,                         -- NULL = use default fee rate
    note        TEXT,
    source      TEXT NOT NULL DEFAULT 'form', -- form, paste, xlsx
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (day, symbol, side, quantity, price)
);

-- Per-position overrides (stop-loss % etc.); NULL means use the default.
CREATE TABLE IF NOT EXISTS position_settings (
    symbol        TEXT PRIMARY KEY,
    stop_loss_pct REAL,
    stop_price    REAL,                       -- ratcheting stop, only moves up
    stop_entry_day TEXT                       -- position the stop belongs to (its first buy day)
);

-- One row per stock per analysed day: the signal log behind every report.
CREATE TABLE IF NOT EXISTS signals (
    day          TEXT NOT NULL,               -- trading day analysed
    symbol       TEXT NOT NULL,
    list         TEXT NOT NULL,               -- holding, watch, scan
    verdict      TEXT NOT NULL,
    score        INTEGER NOT NULL,
    close        REAL,
    stop         REAL,
    target       REAL,
    detail_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (day, symbol)
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol        TEXT PRIMARY KEY,
    target_entry  REAL,
    note          TEXT,
    added_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS announcements (
    id          INTEGER PRIMARY KEY,          -- cse.lk id
    kind        TEXT NOT NULL,                -- approved, financial
    company     TEXT,
    symbol      TEXT,
    category    TEXT,
    title       TEXT,
    announced   TEXT,
    url         TEXT,
    raw_json    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_day      TEXT NOT NULL,
    kind         TEXT NOT NULL,               -- daily, backfill, weekly
    status       TEXT NOT NULL,               -- running, ok, partial, failed
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    message      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Every saved set of strategy settings, newest last; restorable from the Settings page.
CREATE TABLE IF NOT EXISTS settings_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    note         TEXT NOT NULL,
    values_json  TEXT NOT NULL
);

-- Cash dividends, one row per CSE announcement (amounts are per share, LKR).
CREATE TABLE IF NOT EXISTS dividends (
    announcement_id  INTEGER PRIMARY KEY,
    base             TEXT NOT NULL,            -- JKH (applies to JKH.N0000 / JKH.X0000)
    voting_dps       REAL,
    non_voting_dps   REAL,
    xd               TEXT,                     -- YYYY-MM-DD ex-dividend date
    payment          TEXT,
    announced        TEXT,
    remarks          TEXT,
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-stock fundamentals: net assets per share read from the latest interim
-- report, plus values you type in. A manual value wins over the automatic one.
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol        TEXT PRIMARY KEY,
    nav_auto      REAL,
    nav_source    TEXT,                        -- PDF url the automatic NAV came from
    nav_period    TEXT,                        -- e.g. Interim ... ended 30th September 2026
    nav_report_day TEXT,                       -- date of that report (YYYY-MM-DD)
    nav_checked   TEXT,                        -- when that PDF was last read
    nav_manual    REAL,
    eps_ttm       REAL,                        -- trailing 12-month EPS, typed in
    note          TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Market-wide P/E, P/BV and dividend yield from the CSE weekly report.
CREATE TABLE IF NOT EXISTS market_valuation (
    report_day  TEXT PRIMARY KEY,
    per         REAL,
    pbv         REAL,
    dy          REAL,
    source      TEXT
);

-- Headlines matched to a stock. One row per (link, symbol).
CREATE TABLE IF NOT EXISTS news (
    link        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    title       TEXT NOT NULL,
    source      TEXT,
    published   TEXT,                          -- YYYY-MM-DD HH:MM, Colombo
    feed        TEXT NOT NULL,                 -- google, economynext, dailymirror
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (link, symbol)
);

-- Named sets of settings you chose to keep, e.g. "Default", "Wide stops".
CREATE TABLE IF NOT EXISTS settings_presets (
    name         TEXT PRIMARY KEY,
    saved_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    values_json  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "stop_loss_pct": "10",
    "breakeven_at_r": "1",
    "chandelier_atr_mult": "3",
    "partial_take_at_r": "2",
    "fee_rate_pct": "1.12",
    "max_weight_pct": "20",
    "min_turnover_lkr": "1000000",
    "risk_per_trade_pct": "1",
    "enter_rsi_low": "40",
    "enter_rsi_high": "60",
}

# Columns added after a database may already exist: (table, column, type).
MIGRATIONS = [("position_settings", "stop_entry_day", "TEXT"), ("fundamentals", "nav_report_day", "TEXT")]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI may open the connection in a worker thread and use it from the
    # event loop; each request has its own connection, so this is safe.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", DEFAULT_SETTINGS.items()
    )
    conn.commit()
    return conn
