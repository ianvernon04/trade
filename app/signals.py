"""Composite buy/sell signal engine.

Each indicator votes with a weighted score; the total is normalized to
[-100, +100] and mapped to a label. Every vote carries a human-readable
reason so the UI can explain *why* the signal reads the way it does.
"""

from __future__ import annotations

import pandas as pd

from .indicators import compute_all

# (weight, name) — weights sum to 100 for a clean normalized score.
WEIGHTS = {
    "trend": 25,
    "macd": 20,
    "rsi": 15,
    "stochastic": 10,
    "bollinger": 10,
    "momentum": 10,
    "volume": 10,
}


def score_frame(df: pd.DataFrame) -> dict:
    """Score the most recent bar of an indicator-annotated OHLCV frame."""
    ind = compute_all(df)
    row = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else row
    votes: list[dict] = []

    def vote(component: str, direction: float, reason: str):
        # direction in [-1, 1]
        votes.append({
            "component": component,
            "weight": WEIGHTS[component],
            "score": round(WEIGHTS[component] * direction, 2),
            "reason": reason,
        })

    close = row["close"]

    # --- Trend: price vs moving-average stack ---
    if pd.notna(row["sma50"]):
        above50 = close > row["sma50"]
        above200 = pd.isna(row["sma200"]) or close > row["sma200"]
        stack_bull = pd.notna(row["sma20"]) and row["sma20"] > row["sma50"]
        d = (0.5 if above50 else -0.5) + (0.3 if above200 else -0.3) + (0.2 if stack_bull else -0.2)
        vote("trend", d,
             f"Price {'above' if above50 else 'below'} 50-day MA, "
             f"{'above' if above200 else 'below'} 200-day MA, "
             f"20/50 MA stack {'bullish' if stack_bull else 'bearish'}")

    # --- MACD ---
    if pd.notna(row["macd_hist"]):
        rising = row["macd_hist"] > prev["macd_hist"]
        if row["macd_hist"] > 0:
            d = 1.0 if rising else 0.4
            vote("macd", d, f"MACD above signal line and histogram {'expanding' if rising else 'contracting'}")
        else:
            d = -1.0 if not rising else -0.4
            vote("macd", d, f"MACD below signal line and histogram {'contracting' if rising else 'expanding'}")

    # --- RSI (mean-reversion at extremes, confirmation mid-range) ---
    r = row["rsi14"]
    if pd.notna(r):
        if r < 30:
            vote("rsi", 1.0, f"RSI {r:.0f} — oversold, bounce candidate")
        elif r < 45:
            vote("rsi", 0.3, f"RSI {r:.0f} — mildly weak")
        elif r <= 60:
            vote("rsi", 0.1, f"RSI {r:.0f} — neutral")
        elif r <= 70:
            vote("rsi", -0.3, f"RSI {r:.0f} — getting stretched")
        else:
            vote("rsi", -1.0, f"RSI {r:.0f} — overbought, pullback risk")

    # --- Stochastic cross ---
    if pd.notna(row["stoch_k"]) and pd.notna(row["stoch_d"]):
        k, dline = row["stoch_k"], row["stoch_d"]
        if k < 20 and k > dline:
            vote("stochastic", 1.0, f"Stochastic %K {k:.0f} crossing up from oversold")
        elif k > 80 and k < dline:
            vote("stochastic", -1.0, f"Stochastic %K {k:.0f} crossing down from overbought")
        else:
            vote("stochastic", 0.4 if k > dline else -0.4,
                 f"Stochastic %K {'above' if k > dline else 'below'} %D")

    # --- Bollinger position ---
    if pd.notna(row["bb_upper"]) and row["bb_upper"] != row["bb_lower"]:
        pos = (close - row["bb_lower"]) / (row["bb_upper"] - row["bb_lower"])  # 0..1
        if pos < 0.05:
            vote("bollinger", 0.9, "Price at/below lower Bollinger band — stretched down")
        elif pos > 0.95:
            vote("bollinger", -0.9, "Price at/above upper Bollinger band — stretched up")
        else:
            vote("bollinger", (0.5 - pos) * 0.8, f"Price at {pos * 100:.0f}% of Bollinger range")

    # --- Momentum: 10-bar rate of change ---
    if len(ind) > 10:
        roc = (close / ind["close"].iloc[-11] - 1) * 100
        d = max(-1.0, min(1.0, roc / 8))
        vote("momentum", d, f"10-bar momentum {roc:+.1f}%")

    # --- Volume: OBV trend confirmation ---
    if len(ind) > 20 and pd.notna(row["obv"]):
        obv_ma = ind["obv"].rolling(20).mean().iloc[-1]
        d = 0.8 if row["obv"] > obv_ma else -0.8
        vote("volume", d, f"On-balance volume {'above' if d > 0 else 'below'} its 20-bar average")

    total = sum(v["score"] for v in votes)
    used = sum(v["weight"] for v in votes) or 1
    score = round(total / used * 100, 1)

    return {
        "score": score,
        "label": label_for(score),
        "votes": votes,
        "close": round(float(close), 4),
        "atr": round(float(row["atr14"]), 4) if pd.notna(row["atr14"]) else None,
        "rsi": round(float(r), 1) if pd.notna(r) else None,
    }


def label_for(score: float) -> str:
    if score >= 40:
        return "STRONG BUY"
    if score >= 15:
        return "BUY"
    if score <= -40:
        return "STRONG SELL"
    if score <= -15:
        return "SELL"
    return "NEUTRAL"


def score_series(df: pd.DataFrame) -> pd.Series:
    """Vectorized-ish historical score series used by the backtester.

    Recomputing the full vote set per bar would be slow in Python, so this
    uses the same components in vectorized form. It intentionally mirrors
    score_frame's logic closely enough that backtest results are
    representative of the live signal.
    """
    ind = compute_all(df)
    close = ind["close"]
    score = pd.Series(0.0, index=ind.index)
    used = pd.Series(0.0, index=ind.index)

    # Trend
    have = ind["sma50"].notna()
    t = (
        ((close > ind["sma50"]).astype(float) * 2 - 1) * 0.5
        + ((ind["sma200"].isna()) | (close > ind["sma200"])).astype(float).mul(2).sub(1) * 0.3
        + ((ind["sma20"] > ind["sma50"]).astype(float) * 2 - 1) * 0.2
    )
    score += t.where(have, 0) * WEIGHTS["trend"]
    used += have * WEIGHTS["trend"]

    # MACD
    have = ind["macd_hist"].notna()
    rising = ind["macd_hist"] > ind["macd_hist"].shift()
    m = pd.Series(0.0, index=ind.index)
    m[(ind["macd_hist"] > 0) & rising] = 1.0
    m[(ind["macd_hist"] > 0) & ~rising] = 0.4
    m[(ind["macd_hist"] <= 0) & ~rising] = -1.0
    m[(ind["macd_hist"] <= 0) & rising] = -0.4
    score += m.where(have, 0) * WEIGHTS["macd"]
    used += have * WEIGHTS["macd"]

    # RSI
    r = ind["rsi14"]
    have = r.notna()
    rv = pd.Series(0.1, index=ind.index)
    rv[r < 45] = 0.3
    rv[r < 30] = 1.0
    rv[r > 60] = -0.3
    rv[r > 70] = -1.0
    score += rv.where(have, 0) * WEIGHTS["rsi"]
    used += have * WEIGHTS["rsi"]

    # Stochastic
    k, dline = ind["stoch_k"], ind["stoch_d"]
    have = k.notna() & dline.notna()
    sv = pd.Series(0.0, index=ind.index)
    sv[k > dline] = 0.4
    sv[k <= dline] = -0.4
    sv[(k < 20) & (k > dline)] = 1.0
    sv[(k > 80) & (k < dline)] = -1.0
    score += sv.where(have, 0) * WEIGHTS["stochastic"]
    used += have * WEIGHTS["stochastic"]

    # Bollinger
    width = ind["bb_upper"] - ind["bb_lower"]
    have = ind["bb_upper"].notna() & (width != 0)
    pos = (close - ind["bb_lower"]) / width
    bv = ((0.5 - pos) * 0.8).clip(-0.9, 0.9)
    bv[pos < 0.05] = 0.9
    bv[pos > 0.95] = -0.9
    score += bv.where(have, 0) * WEIGHTS["bollinger"]
    used += have * WEIGHTS["bollinger"]

    # Momentum
    roc = (close / close.shift(10) - 1) * 100
    have = roc.notna()
    score += (roc / 8).clip(-1, 1).where(have, 0) * WEIGHTS["momentum"]
    used += have * WEIGHTS["momentum"]

    # Volume
    obv_ma = ind["obv"].rolling(20).mean()
    have = obv_ma.notna()
    vv = ((ind["obv"] > obv_ma).astype(float) * 2 - 1) * 0.8
    score += vv.where(have, 0) * WEIGHTS["volume"]
    used += have * WEIGHTS["volume"]

    return (score / used.replace(0, 1) * 100).round(1)
