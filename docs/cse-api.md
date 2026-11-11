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
| `getAnnouncementByCompany` | `symbol`, `fromDate`, `toDate` (YYYY-MM-DD) | `reqCompanyAnnouncement[]`: as `approvedAnnouncement`, `symbol` null | One company's announcements (verified 2026-11-11). |
| `getAnnouncementById` | `announcementId` (not `id`) | `reqBaseAnnouncement`: `symbol`, `votingDivPerShare`, `nonVotingDivPerShare`, `xd`, `payment` (`DD Mon YYYY`), `remarks` | Dividend amount and ex-date. |
| `financials` | `symbol` | `infoQuarterlyData[]`, `infoAnnualData[]`: `path`, `fileText`, `manualDate`, `uploadedDate`; `reqFinancial` has no figures | Latest interim report PDF. |
| `cseWeekly` | – | `reqCseWeekly[]`: `path`, `file_text`, `manual_date` | CSE weekly report; page 1 has market PER, PBV, DY. Per-company ratios are not published. |
| `aspiData`, `snpData`, `marketSummery` | – | Today's index value / market totals | Not stored yet. |

## Gotchas

- Today's bar exists during trading hours and is partial. The collector only
  keeps a day once it is before today or after 14:45 Colombo time.
- Daily bars have no open price.
- Timestamps are epoch ms; convert in `Asia/Colombo`.
- Approved announcements carry a company name but `symbol` is null; it is
  matched to a stock by normalised name.
- Report `path`s may lack the `cmt/` prefix; the file is always at
  `https://cdn.cse.lk/cmt/...` (without it the CDN returns an XML error).
- `manualDate` on a report is typed by hand and can be wrong (a 2020 report
  dated 2030 was seen); sort by `uploadedDate`.
