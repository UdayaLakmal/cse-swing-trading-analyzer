# cse-swing-trading-analyzer

CSE Swing-Trading Analyzer: pulls end-of-day Colombo Stock Exchange data,
scores holdings (Hold / Add / Trim / Sell) and watchlist stocks
(Enter / Wait / Avoid), and shows a local report at http://localhost:8080.
Decision support only; it never places trades.

## Status

| Phase | State |
|---|---|
| 1. Data probe + 1-year backfill | Done |
| 2. Core app: trade import, holdings, P/L | Done |
| 3. Daily technical analysis + report | Done |
| 4. systemd timer + desktop notification | Not started |
| 5. Weekly fundamentals and news | Announcements stored; scoring not started |
| 6. Backtest, signal log, regime filter, AI summary | Not started |

## Run

```bash
export UID GID=$(id -g)
docker compose build
docker compose run --rm job backfill   # first time: ~1 year of history, ~4 min
docker compose run --rm job daily      # fetch + analyse; skips if today already ran; --force to rerun
docker compose run --rm job analyze    # re-score stored prices only (seconds)
docker compose run --rm job status
docker compose up -d web               # http://localhost:8080
```

Data lives in `./data/cse.db` (SQLite, git-ignored).

## Develop

```bash
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "cp -r /src /w && cd /w && pip install -q '.[dev]' && pytest -q && python scripts/probe.py"
```

`scripts/probe.py` checks the cse.lk endpoints still return the fields we use;
see [docs/cse-api.md](docs/cse-api.md).

## Layout

- `src/cse_analyzer/cse_client.py` – cse.lk API client and parsers
- `src/cse_analyzer/collector.py` – fetch + store, catch-up guard, staleness check
- `src/cse_analyzer/db.py` – SQLite schema and default settings (10% stop, 3x ATR trail, 1.12% fees…)
- `src/cse_analyzer/cli.py` – `cse backfill | daily | analyze | status`
- `src/cse_analyzer/trades.py` – symbol matching, paste and .xlsx parsing, duplicate-safe saving
- `src/cse_analyzer/portfolio.py` – holdings, average cost, net P/L, break-even, stops, weights
- `src/cse_analyzer/indicators.py` – moving averages, RSI, MACD, ATR (pandas only)
- `src/cse_analyzer/analysis.py` – score, verdicts, trailing stops, regime filter, signal log
- `src/cse_analyzer/charts.py` – server-side SVG charts (work offline)
- `src/cse_analyzer/web.py` + `templates/` – Today, Holdings, Watchlist, Market scan, stock pages, Trades, Settings

## How the analysis works

Each stock gets a score from -100 to +100 built from visible rules: trend
(price vs 20/50/200-day averages, 50 vs 200), momentum (RSI 14, MACD), volume
(big up or down days), relative strength vs the ASPI over 1 and 3 months, and
levels (52-week high/low, 20-day breakouts). The stock page lists every rule
and its points.

| List | Verdicts | Rule |
|---|---|---|
| Holdings | Sell | Close below the stop |
| | Trim | Reached +2R and no partial sale yet, or above the max weight |
| | Add | Score ≥ 40, in profit, market uptrend, liquid |
| | Hold | Otherwise |
| Watchlist and scan | Avoid | Thin turnover, long-term downtrend, or score ≤ -20 |
| | Enter | Above the 50-day, RSI in the entry zone, volume at or above normal, market uptrend |
| | Wait | Otherwise (the page says what is missing) |

Stops: entry minus the stop % (default 10%); after +1R the stop moves to
break-even plus fees, then trails the highest close since entry by 3x ATR(14).
The stop is stored and never moves down. Enter signals are muted to Wait when
the ASPI is below its 200-day average. Every number above is editable on the
Settings page, and every daily verdict is kept in the `signals` table.

## Entering trades

On the Trades page, any of:

- **Form**: one buy or sell.
- **Paste**: one per line, e.g. `JKH 500 @ 19.50`, `SELL COMB 100 @ 120 2026-11-01`,
  `2026-11-03 B HNB 1,000 @ 310 fees 3500`. Side defaults to BUY, date to today,
  dates may be `YYYY-MM-DD` or `DD/MM/YYYY`.
- **.xlsx**: download the template (Date, Symbol, Side, Quantity, Price, Fees, Note).
  Re-uploading the same sheet adds only new rows.
- **Broker statement**: upload the broker's Portfolio export as it is (columns
  Security, Quantity, Avg Price, Total Cost). Each stock not already held in the
  app becomes one opening BUY dated today. The broker's Avg Price includes buy
  fees, so it is split into price (Avg ÷ 1.0112) and fees, and the cost basis
  equals the broker's Total Cost. Stocks already held are never added again;
  the preview flags any quantity that differs from the statement, and any stock
  the app holds that the statement does not.

Both paste and upload show a preview before anything is saved. Bare tickers
resolve to the voting share (`JKH` → `JKH.N0000`). Blank fees use the default
rate (1.12% per side). A trade identical in date, symbol, side, quantity and
price to a saved one is treated as a duplicate. If you really made two
identical trades on one day, enter them as one trade with the combined quantity.
