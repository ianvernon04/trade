"""Technical indicators, implemented with pandas/numpy (no external TA lib)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    std = close.rolling(n).std()
    return mid + k * std, mid, mid - k * std


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    k = 100 * (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan)
    return k, k.rolling(d).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the full indicator set as columns to an OHLCV frame."""
    out = df.copy()
    close = out["close"]
    out["sma20"] = sma(close, 20)
    out["sma50"] = sma(close, 50)
    out["sma200"] = sma(close, 200)
    out["ema9"] = ema(close, 9)
    out["ema21"] = ema(close, 21)
    out["rsi14"] = rsi(close)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(close)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bollinger(close)
    out["stoch_k"], out["stoch_d"] = stochastic(out)
    out["atr14"] = atr(out)
    out["obv"] = obv(out)
    return out
