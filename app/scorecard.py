"""Agent scorecard: grades the signal's own historical accuracy.

For every day the composite score crossed the buy (or sell) threshold in the
past N years, measure the forward return over a fixed horizon and report the
win rate. This is the app holding itself accountable: it tells you how often
this exact signal was right on this exact ticker before you trust it.
"""

from __future__ import annotations

from . import data
from .signals import score_frame, score_series


def signal_scorecard(ticker: str, buy_threshold: float = 20, sell_threshold: float = -15,
                     horizon: int = 10, period: str = "3y") -> dict:
    df = data.get_history(ticker, period, "1d")
    scores = score_series(df)
    close = df["close"]

    def collect(kind: str) -> dict:
        events = []
        for i in range(1, len(df) - 1):
            s, prev = scores.iloc[i], scores.iloc[i - 1]
            if kind == "buy":
                crossed = s >= buy_threshold and prev < buy_threshold
            else:
                crossed = s <= sell_threshold and prev > sell_threshold
            if not crossed:
                continue
            j = min(i + horizon, len(df) - 1)
            fwd = float(close.iloc[j] / close.iloc[i] - 1)
            win = fwd > 0 if kind == "buy" else fwd < 0
            events.append({
                "date": str(df.index[i].date()),
                "score": float(scores.iloc[i]),
                "fwd_return_pct": round(fwd * 100, 2),
                "win": bool(win),
            })
        wins = [e for e in events if e["win"]]
        rets = [e["fwd_return_pct"] for e in events]
        return {
            "count": len(events),
            "win_rate": round(len(wins) / len(events) * 100, 1) if events else None,
            "avg_fwd_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
            "recent": events[-8:][::-1],
        }

    return {
        "ticker": ticker.upper(),
        "period": period,
        "horizon_days": horizon,
        "buy_signals": collect("buy"),
        "sell_signals": collect("sell"),
        "note": (f"Every historical crossing of the live signal's thresholds over the past "
                 f"{period}, graded on the {horizon}-bar forward move. Past accuracy never "
                 "guarantees future results — use this to calibrate trust, not as a promise."),
    }


def weekly_signal(ticker: str) -> dict:
    """Weekly-timeframe signal for confluence checks."""
    df = data.get_history(ticker, "2y", "1wk")
    return score_frame(df)


def confluence(daily: dict, weekly: dict) -> dict:
    d, w = daily["score"], weekly["score"]
    d_dir = 1 if d >= 15 else -1 if d <= -15 else 0
    w_dir = 1 if w >= 15 else -1 if w <= -15 else 0
    if d_dir and d_dir == w_dir:
        state, note = "agree", "Daily and weekly timeframes agree — higher-conviction setup"
    elif d_dir and w_dir and d_dir != w_dir:
        state, note = "conflict", "Daily and weekly timeframes conflict — lower conviction, size down or skip"
    else:
        state, note = "mixed", "One timeframe is neutral — no multi-timeframe confirmation"
    return {"weekly_score": w, "weekly_label": weekly["label"], "state": state, "note": note}
