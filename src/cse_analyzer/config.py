"""Runtime settings, read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

COLOMBO = ZoneInfo("Asia/Colombo")

# CSE trading hours are 09:30-14:30 Colombo time. A day's bar is treated as
# final only after this time, so a 10 am run never stores a half-day bar.
MARKET_CLOSE_FINAL = (14, 45)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    cse_base_url: str
    request_delay_s: float
    http_timeout_s: float


def load_settings() -> Settings:
    return Settings(
        db_path=Path(os.environ.get("CSE_DB_PATH", "data/cse.db")),
        cse_base_url=os.environ.get("CSE_BASE_URL", "https://www.cse.lk/api"),
        request_delay_s=float(os.environ.get("CSE_REQUEST_DELAY_S", "0.6")),
        http_timeout_s=float(os.environ.get("CSE_HTTP_TIMEOUT_S", "30")),
    )
