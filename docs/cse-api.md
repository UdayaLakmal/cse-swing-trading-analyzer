# cse.lk API notes

Unofficial, undocumented. Base `https://www.cse.lk/api/`, every call is `POST`
with a form-encoded body, no auth. Verified live on **2026-11-10**; re-check with
`python scripts/probe.py`.

| Endpoint | Body | Returns | Used for |
|---|---|---|---|
| `tradeSummary` | – | `reqTradeSummery[]`: `id`, `symbol`, `name`, `price`, `previousClose`, `high`, `low`, `turnover`, `sharevolume`, `marketCap`, `lastTradedTime` (ms) | Stock list (~293). `id` is the `stockId` for charts. |
| `companyChartDataByStock` | `stockId`, `period` | `chartData[]`: `t` (ms), `h`, `l`, `p` (close), `q` (volume incl. crossings); `o` is always null | Daily bars. `period`: 1 intraday, 2 ≈1 week, 3 ≈1 month, 4 ≈3 months, 5 ≈1 year (≈244 bars). |
| `chartData` | `chartId`, `period` | `[]` of `d` (ms), `v`, `pc` | Index history. `chartId` 1 = ASPI, 40 = S&P SL20. |
| `companyInfoSummery` | `symbol` (e.g. `JKH.N0000`) | `reqSymbolInfo` (52-week hi/lo `p12HiPrice`/`p12LowPrice`, volumes), `reqSymbolBetaInfo` | Per-stock detail, beta. |
| `approvedAnnouncement` | – | `approvedAnnouncements[]`: `id`, `company`, `announcementCategory`, `dateOfAnnouncement`, `xd`, `recordDate`… | Dividends, rights, director dealings. |
| `getFinancialAnnouncement` | – | `reqFinancialAnnouncemnets[]`: `id`, `symbol`, `name`, `fileText`, `path` | Interim reports. PDF at `https://cdn.cse.lk/<path>`. |
| `aspiData`, `snpData`, `marketSummery` | – | Today's index value / market totals | Not stored yet. |

## Gotchas

- Today's bar exists during trading hours and is partial. The collector only
  keeps a day once it is before today or after 14:45 Colombo time.
- Daily bars have no open price.
- Timestamps are epoch ms; convert in `Asia/Colombo`.
