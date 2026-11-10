"""Technical indicators on daily bars (pandas only, no TA library).

Every function takes a DataFrame indexed by day with columns high, low,
close, volume (CSE daily bars have no open) and returns a Series aligned to it.
"""

from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    # No losses at all in the window -> RSI 100; no gains -> 0.
    out = out.where(loss != 0, 100.0)
    return out.where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's ATR. Missing high/low fall back to the close."""
    high = df["high"].fillna(df["close"])
    low = df["low"].fillna(df["close"])
    prev = df["close"].shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def pct_change(s: pd.Series, n: int) -> float | None:
    if len(s) <= n or pd.isna(s.iloc[-n - 1]) or s.iloc[-n - 1] == 0:
        return None
    return 100 * (s.iloc[-1] / s.iloc[-n - 1] - 1)
