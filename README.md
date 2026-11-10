# cse-swing-trading-analyzer

CSE Swing-Trading Analyzer: pulls end-of-day Colombo Stock Exchange data,
scores holdings (Hold / Add / Trim / Sell) and watchlist stocks
(Enter / Wait / Avoid), and shows a local report at http://localhost:8080.
Decision support only; it never places trades.

## Status

| Phase | State |
|---|---|
| 1. Data probe + 1-year backfill | Done |
| 2. Core app: trade import, holdings, P/L | Schema in place; UI next |
| 3. Daily technical analysis + report | Not started |
| 4. systemd timer + desktop notification | Not started |
| 5. Weekly fundamentals and news | Announcements stored; scoring not started |
| 6. Backtest, signal log, regime filter, AI summary | Not started |

## Run

```bash
export UID GID=$(id -g)
docker compose build
docker compose run --rm job backfill   # first time: ~1 year of history, ~4 min
docker compose run --rm job daily      # skips if today already ran; --force to rerun
docker compose run --rm job status
docker compose up -d web               # http://localhost:8080/api/status
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
- `src/cse_analyzer/cli.py` – `cse backfill | daily | status`
- `src/cse_analyzer/web.py` – FastAPI app (stub)
