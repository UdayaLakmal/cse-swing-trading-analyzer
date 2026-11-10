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
    stop_price    REAL                        -- ratcheting stop, only moves up
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
"""

DEFAULT_SETTINGS = {
    "stop_loss_pct": "10",
    "breakeven_at_r": "1",
    "chandelier_atr_mult": "3",
    "partial_take_at_r": "2",
    "fee_rate_pct": "1.12",
    "max_weight_pct": "20",
    "min_turnover_lkr": "1000000",
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", DEFAULT_SETTINGS.items()
    )
    conn.commit()
    return conn
