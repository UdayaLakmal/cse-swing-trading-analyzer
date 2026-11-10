"""Check that the cse.lk endpoints still answer with the fields we rely on.

Run: python scripts/probe.py   (exit code 1 if anything looks wrong)
"""

import sys

import httpx

BASE = "https://www.cse.lk/api"

CHECKS = [
    # (endpoint, form data, path to the list or dict, keys we read)
    ("tradeSummary", {}, "reqTradeSummery", {"id", "symbol", "name", "price", "previousClose", "turnover"}),
    ("companyChartDataByStock", {"stockId": 297, "period": 5}, "chartData", {"t", "h", "l", "p", "q"}),
    ("chartData", {"chartId": 1, "period": 5}, None, {"d", "v"}),
    ("chartData", {"chartId": 40, "period": 5}, None, {"d", "v"}),
    ("companyInfoSummery", {"symbol": "JKH.N0000"}, "reqSymbolInfo", {"symbol", "lastTradedPrice", "p12HiPrice"}),
    ("approvedAnnouncement", {}, "approvedAnnouncements", {"id", "company", "announcementCategory"}),
    ("getFinancialAnnouncement", {}, "reqFinancialAnnouncemnets", {"id", "symbol", "path", "fileText"}),
    ("aspiData", {}, None, {"value", "change"}),
    ("marketSummery", {}, None, {"tradeVolume", "shareVolume"}),
]


def main() -> int:
    ok = True
    with httpx.Client(timeout=30) as http:
        for endpoint, data, key, fields in CHECKS:
            label = f"{endpoint} {data or ''}".strip()
            try:
                body = http.post(f"{BASE}/{endpoint}", data=data).raise_for_status().json()
                item = body[key] if key else body
                sample = item[0] if isinstance(item, list) else item
                n = len(item) if isinstance(item, list) else 1
                missing = fields - set(sample)
                if n == 0 or missing:
                    ok = False
                    print(f"FAIL {label}: rows={n} missing={sorted(missing)}")
                else:
                    print(f"ok   {label}: rows={n}")
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"FAIL {label}: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
