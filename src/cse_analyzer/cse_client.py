"""Thin client for the unofficial cse.lk JSON API.

All endpoints are POST with form-encoded bodies and no auth. They are
undocumented, so every parser here is defensive and docs/cse-api.md records
what was verified and when.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from .config import COLOMBO

# chartData ids for market indices (verified 2026-11-10).
INDEX_CHART_IDS = {"ASPI": 1, "SNPSL20": 40}

# companyChartDataByStock periods: 1 = intraday, 2 = ~1 week, 3 = ~1 month,
# 4 = ~3 months, 5 = ~1 year of daily bars.
PERIOD_WEEK = 2
PERIOD_YEAR = 5


class CseApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class StockSummary:
    stock_id: int
    symbol: str
    name: str
    price: float | None
    previous_close: float | None
    turnover: float | None
    market_cap: float | None
    last_traded: datetime | None


@dataclass(frozen=True)
class Bar:
    """One daily bar. `ts` is the raw timestamp of the bar in Colombo time."""

    ts: datetime
    high: float | None
    low: float | None
    close: float | None
    volume: int | None

    @property
    def day(self) -> date:
        return self.ts.date()


@dataclass(frozen=True)
class IndexPoint:
    ts: datetime
    value: float
    change_pct: float | None


def _ms_to_colombo(ms: int | float | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=COLOMBO)


def parse_trade_summary(payload: dict) -> list[StockSummary]:
    rows = payload.get("reqTradeSummery")
    if not isinstance(rows, list):
        raise CseApiError("tradeSummary: missing reqTradeSummery list")
    out = []
    for r in rows:
        if not r.get("symbol") or r.get("id") is None:
            continue
        out.append(
            StockSummary(
                stock_id=int(r["id"]),
                symbol=r["symbol"],
                name=r.get("name") or r["symbol"],
                price=r.get("price"),
                previous_close=r.get("previousClose"),
                turnover=r.get("turnover"),
                market_cap=r.get("marketCap"),
                last_traded=_ms_to_colombo(r.get("lastTradedTime")),
            )
        )
    return out


def parse_stock_chart(payload: dict) -> list[Bar]:
    rows = payload.get("chartData")
    if not isinstance(rows, list):
        raise CseApiError("companyChartDataByStock: missing chartData list")
    bars = []
    for r in rows:
        ts = _ms_to_colombo(r.get("t"))
        if ts is None:
            continue
        # p = close/last price, q = share volume (incl. crossings), o is null.
        bars.append(Bar(ts=ts, high=r.get("h"), low=r.get("l"), close=r.get("p"), volume=r.get("q")))
    return bars


def parse_index_chart(payload: list) -> list[IndexPoint]:
    if not isinstance(payload, list):
        raise CseApiError("chartData: expected a list")
    return [
        IndexPoint(ts=_ms_to_colombo(r["d"]), value=r["v"], change_pct=r.get("pc"))
        for r in payload
        if r.get("d") is not None and r.get("v") is not None
    ]


class CseClient:
    def __init__(self, base_url: str, delay_s: float = 0.6, timeout_s: float = 30.0):
        self._base = base_url.rstrip("/")
        self._delay = delay_s
        self._last_call = 0.0
        self._http = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": "cse-analyzer/0.1 (personal EOD research tool)"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CseClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _post(self, endpoint: str, data: dict | None = None, retries: int = 3):
        for attempt in range(retries):
            wait = self._delay - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self._http.post(f"{self._base}/{endpoint}", data=data or {})
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt == retries - 1:
                    raise CseApiError(f"{endpoint} failed: {e}") from e
                time.sleep(2 ** attempt)

    def trade_summary(self) -> list[StockSummary]:
        return parse_trade_summary(self._post("tradeSummary"))

    def stock_chart(self, stock_id: int, period: int = PERIOD_YEAR) -> list[Bar]:
        return parse_stock_chart(self._post("companyChartDataByStock", {"stockId": stock_id, "period": period}))

    def index_chart(self, index_code: str, period: int = PERIOD_YEAR) -> list[IndexPoint]:
        chart_id = INDEX_CHART_IDS[index_code]
        return parse_index_chart(self._post("chartData", {"chartId": chart_id, "period": period}))

    def company_info(self, symbol: str) -> dict:
        return self._post("companyInfoSummery", {"symbol": symbol})

    def approved_announcements(self) -> list[dict]:
        return self._post("approvedAnnouncement").get("approvedAnnouncements", [])

    def financial_announcements(self) -> list[dict]:
        return self._post("getFinancialAnnouncement").get("reqFinancialAnnouncemnets", [])
