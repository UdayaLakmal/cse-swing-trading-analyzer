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
| 4. systemd timer + desktop notification | Done |
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

## Daily schedule and notification

```bash
./scripts/install-timer.sh               # weekdays 10:00 Colombo time
./scripts/install-timer.sh --uninstall
```

This installs a systemd user timer that runs `scripts/cse-daily.sh` on weekdays
at 10:00 Asia/Colombo time. If the laptop was off or asleep at 10:00, it runs as
soon as the laptop is back (`Persistent=true`). The script waits for Docker and
for www.cse.lk, starts the web report if needed, fetches and analyses, then shows
a desktop notification such as "CSE: 1 Sell · 2 Trim" listing the stocks. Click
it, or its "Open report" button, to open http://localhost:8080. A notification
with a Sell stays on screen until you dismiss it. If the run fails, you get a
"CSE daily run failed" notification instead. A second run on the same day does
nothing, so catch-up runs never notify twice.

```bash
systemctl --user list-timers cse-daily.timer   # when it runs next
journalctl --user -u cse-daily -n 50           # last run's log
./scripts/cse-daily.sh --force                 # run now by hand
docker compose --profile job run --rm job summary   # the notification text, as JSON
```

The 10:00 run picks up the previous trading day's close; today's bar is only
final after 14:45.

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
- `src/cse_analyzer/cli.py` – `cse backfill | daily | analyze | status | summary`
- `src/cse_analyzer/notify.py` – the daily notification text
- `scripts/cse-daily.sh`, `scripts/install-timer.sh`, `deploy/systemd/` – timer, run script, notification
- `src/cse_analyzer/trades.py` – symbol matching, paste and .xlsx parsing, duplicate-safe saving
- `src/cse_analyzer/portfolio.py` – holdings, average cost, net P/L, break-even, stops, weights
- `src/cse_analyzer/indicators.py` – moving averages, RSI, MACD, ATR (pandas only)
- `src/cse_analyzer/analysis.py` – score, verdicts, trailing stops, regime filter, signal log
- `src/cse_analyzer/charts.py` – server-side SVG charts (work offline)
- `src/cse_analyzer/strategy.py` – Strategy page data: rule hit counts, exit ladders, what-if re-runs
- `src/cse_analyzer/web.py` + `templates/` – Today, Holdings, Watchlist, Market scan, stock pages, Trades, Strategy, Settings

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

The **Strategy** page (`/strategy`) explains all of this against today's
signals, to help tune it: the strategy in five steps with your settings, a
per-group score breakdown for each holding and watchlist stock, each holding's
exit ladder (initial stop, break-even trigger, trim target, current stop), how
many stocks each verdict rule and score rule decided today, which entry check
holds stocks back (and the near misses), the score spread, and a what-if table
that re-runs today's verdicts with one setting changed without saving anything.

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
