"""Market data layer.

Pulls live quotes, historical candles, and options chains from Yahoo Finance
(via yfinance), with a small in-memory TTL cache so the UI can poll
aggressively without hammering the upstream source.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import pandas as pd
import yfinance as yf

# Default watchlist: large-cap / high-volume technology names with liquid options.
DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    "AMD", "AVGO", "PLTR", "QQQ", "SPY",
]

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, fn):
    """Return cached value for key if fresh, else compute, store and return."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def get_quote(ticker: str) -> dict:
    """Live-ish quote (cached 20s)."""
    ticker = ticker.upper().strip()

    def fetch():
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = info.last_price
        prev = info.previous_close
        change = (price - prev) if (price is not None and prev) else None
        return {
            "ticker": ticker,
            "price": _round(price),
            "previous_close": _round(prev),
            "change": _round(change),
            "change_pct": _round(change / prev * 100) if (change is not None and prev) else None,
            "day_high": _round(info.day_high),
            "day_low": _round(info.day_low),
            "open": _round(info.open),
            "volume": info.last_volume,
            "market_cap": getattr(info, "market_cap", None),
            "year_high": _round(getattr(info, "year_high", None)),
            "year_low": _round(getattr(info, "year_low", None)),
            "currency": getattr(info, "currency", "USD"),
            "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    return _cached(f"quote:{ticker}", 20, fetch)


def get_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Historical OHLCV candles (cached 120s intraday, 300s daily)."""
    ticker = ticker.upper().strip()
    ttl = 120 if interval.endswith(("m", "h")) else 300

    def fetch():
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"No historical data returned for {ticker}")
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index.name = "date"
        return df

    return _cached(f"hist:{ticker}:{period}:{interval}", ttl, fetch)


def get_option_expiries(ticker: str) -> list[str]:
    ticker = ticker.upper().strip()

    def fetch():
        return list(yf.Ticker(ticker).options or [])

    return _cached(f"expiries:{ticker}", 600, fetch)


def get_option_chain(ticker: str, expiry: Optional[str] = None) -> dict:
    """Raw option chain for one expiry (cached 60s). Returns dict of DataFrames."""
    ticker = ticker.upper().strip()
    expiries = get_option_expiries(ticker)
    if not expiries:
        raise ValueError(f"{ticker} has no listed options")
    if expiry is None or expiry not in expiries:
        expiry = expiries[0]

    def fetch():
        chain = yf.Ticker(ticker).option_chain(expiry)
        return {"expiry": expiry, "calls": chain.calls, "puts": chain.puts}

    return _cached(f"chain:{ticker}:{expiry}", 60, fetch)


def _round(x, n: int = 4):
    try:
        return round(float(x), n) if x is not None else None
    except (TypeError, ValueError):
        return None
