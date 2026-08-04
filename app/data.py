"""Market data layer.

Pulls live quotes, historical candles, and options chains from Yahoo Finance
(via yfinance), with a small in-memory TTL cache so the UI can poll
aggressively without hammering the upstream source.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import logging

import pandas as pd
import yfinance as yf

# yfinance prints its own errors (e.g. 404s for symbols without fundamentals);
# we handle failures ourselves, so keep its console output quiet.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Default watchlist: large-cap / high-volume technology names with liquid options.
DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    "AMD", "AVGO", "PLTR", "QQQ", "SPY",
]

# ETFs have no earnings reports — never ask Yahoo for their earnings dates.
ETF_TICKERS = {
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "SMH", "SOXX",
    "XLK", "XLF", "XLE", "ARKK", "TQQQ", "SQQQ", "GLD", "TLT",
}

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, fn):
    """Return cached value for key if fresh, else compute, store and return.

    Stale-while-error: if the refresh fails (Yahoo rate limits burst-heavy
    hosted traffic) and any previous value exists, serve the stale value
    instead of surfacing an error — a slightly old quote beats a dead UI.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        value = fn()
    except Exception:
        if hit is not None:
            return hit[1]
        raise
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

    return _cached(f"quote:{ticker}", 45, fetch)


def get_quotes_batch(tickers: list[str]) -> dict[str, dict]:
    """Quotes for many tickers in ONE Yahoo request (cached 45s).

    yf.download fetches daily bars for all symbols in a single call — the
    current day's close updates with the live price during market hours.
    Vastly fewer requests than per-ticker fast_info, which matters on
    hosted IPs that Yahoo rate-limits aggressively.
    """
    syms = sorted({t.upper().strip() for t in tickers if t.strip()})
    key = "batch:" + ",".join(syms)

    def fetch():
        df = yf.download(syms, period="5d", interval="1d", group_by="ticker",
                         auto_adjust=True, progress=False, threads=False)
        out: dict[str, dict] = {}
        for s in syms:
            try:
                sub = df[s] if len(syms) > 1 else df
                sub = sub.dropna(subset=["Close"])
                if sub.empty:
                    continue
                last = sub.iloc[-1]
                prev_close = float(sub["Close"].iloc[-2]) if len(sub) > 1 else None
                price = float(last["Close"])
                change = price - prev_close if prev_close else None
                out[s] = {
                    "ticker": s,
                    "price": _round(price),
                    "previous_close": _round(prev_close),
                    "change": _round(change),
                    "change_pct": _round(change / prev_close * 100) if (change is not None and prev_close) else None,
                    "day_high": _round(last.get("High")),
                    "day_low": _round(last.get("Low")),
                    "open": _round(last.get("Open")),
                    "volume": int(last["Volume"]) if last.get("Volume") == last.get("Volume") else None,
                    "market_cap": None, "year_high": None, "year_low": None,
                    "currency": "USD",
                    "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            except Exception:
                continue
        if not out:
            raise ValueError("batch quote fetch returned no data")
        return out

    return _cached(key, 45, fetch)


def _retry(fn, attempts: int = 3, base_delay: float = 1.5):
    """Retry a Yahoo call with exponential backoff on transient/rate-limit errors."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = ("429" in msg or "rate" in msg or "timed out" in msg
                         or "timeout" in msg or "connection" in msg or "curl" in msg)
            if not transient or i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))
    raise last  # unreachable, kept for clarity


def get_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Historical OHLCV candles (cached 300s daily, 120s intraday)."""
    ticker = ticker.upper().strip()
    ttl = 120 if interval.endswith(("m", "h")) else 300

    def fetch():
        def once():
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
            if df is None or df.empty:
                raise ValueError(f"No historical data returned for {ticker}")
            return df
        df = _retry(once)
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


def get_earnings_info(ticker: str) -> dict:
    """Next earnings date and days until it (cached 6h). Best-effort:
    returns {"next_earnings": None, ...} when Yahoo has nothing listed."""
    ticker = ticker.upper().strip()
    if ticker in ETF_TICKERS:
        return {"next_earnings": None, "days_until": None}

    def fetch():
        from datetime import date, datetime
        today = date.today()
        candidates: list[date] = []
        t = yf.Ticker(ticker)
        try:
            cal = t.calendar
            raw = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
            for d in raw or []:
                candidates.append(d.date() if isinstance(d, datetime) else d)
        except Exception:
            pass
        if not candidates:
            try:
                edf = t.get_earnings_dates(limit=8)
                if edf is not None:
                    candidates = [ts.date() for ts in edf.index]
            except Exception:
                pass
        future = sorted(d for d in candidates if d >= today)
        if not future:
            return {"next_earnings": None, "days_until": None}
        nxt = future[0]
        return {"next_earnings": nxt.isoformat(), "days_until": (nxt - today).days}

    try:
        return _cached(f"earnings:{ticker}", 6 * 3600, fetch)
    except Exception:
        return {"next_earnings": None, "days_until": None}


def _round(x, n: int = 4):
    try:
        return round(float(x), n) if x is not None else None
    except (TypeError, ValueError):
        return None
