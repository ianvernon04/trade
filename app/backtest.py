"""Backtesting engine for the composite signal strategy.

Simulates: go long when the score crosses above the buy threshold, exit on
ATR-based stop/target, opposite signal, or max holding period. Reports the
stats an options trader actually cares about (win rate, profit factor,
expectancy, drawdown) plus an option-proxy P&L that approximates trading a
~0.35-delta contract instead of stock.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import data
from .indicators import atr
from .signals import score_series


def run_backtest(
    ticker: str,
    period: str = "2y",
    interval: str = "1d",
    buy_threshold: float = 20,
    sell_threshold: float = -15,
    stop_atr: float = 1.5,
    target_atr: float = 3.0,
    max_hold: int = 15,
    direction: str = "long",
) -> dict:
    df = data.get_history(ticker, period, interval)
    scores = score_series(df)
    a = atr(df)
    close = df["close"]

    sign = 1 if direction == "long" else -1
    enter_th = buy_threshold if direction == "long" else -buy_threshold
    exit_th = sell_threshold if direction == "long" else -sell_threshold

    trades = []
    in_pos = False
    entry_px = entry_i = stop = target = None

    for i in range(1, len(df)):
        px = float(close.iloc[i])
        s = scores.iloc[i]
        date = df.index[i]

        if not in_pos:
            crossed = (s >= enter_th) if sign == 1 else (s <= enter_th)
            prev_ok = (scores.iloc[i - 1] < enter_th) if sign == 1 else (scores.iloc[i - 1] > enter_th)
            if crossed and prev_ok and not np.isnan(a.iloc[i]):
                in_pos = True
                entry_px, entry_i = px, i
                stop = px - sign * stop_atr * float(a.iloc[i])
                target = px + sign * target_atr * float(a.iloc[i])
        else:
            exit_reason = None
            lo, hi = float(df["low"].iloc[i]), float(df["high"].iloc[i])
            if (sign == 1 and lo <= stop) or (sign == -1 and hi >= stop):
                exit_px, exit_reason = stop, "stop"
            elif (sign == 1 and hi >= target) or (sign == -1 and lo <= target):
                exit_px, exit_reason = target, "target"
            elif (sign == 1 and s <= exit_th) or (sign == -1 and s >= exit_th):
                exit_px, exit_reason = px, "signal flip"
            elif i - entry_i >= max_hold:
                exit_px, exit_reason = px, "time"
            if exit_reason:
                ret = sign * (exit_px / entry_px - 1)
                trades.append({
                    "entry_date": str(df.index[entry_i].date()),
                    "exit_date": str(date.date()),
                    "entry": round(entry_px, 2),
                    "exit": round(exit_px, 2),
                    "return_pct": round(ret * 100, 2),
                    "bars_held": i - entry_i,
                    "exit_reason": exit_reason,
                })
                in_pos = False

    return _stats(ticker, df, scores, trades, direction, period, interval)


def _stats(ticker, df, scores, trades, direction, period, interval) -> dict:
    rets = np.array([t["return_pct"] / 100 for t in trades])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]

    equity = [1.0]
    for r in rets:
        equity.append(equity[-1] * (1 + r))
    eq = np.array(equity)
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / peak).min()) if len(eq) > 1 else 0.0

    # Rough option-proxy: a ~0.35-delta contract behaves like ~6-8x leveraged
    # stock exposure on premium at these holding periods. Conservative 5x used.
    opt_lev = 5.0
    opt_equity = [1.0]
    for r in rets:
        opt_equity.append(opt_equity[-1] * max(0.05, 1 + r * opt_lev))

    buy_hold = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100

    return {
        "ticker": ticker.upper(),
        "direction": direction,
        "period": period,
        "interval": interval,
        "bars": len(df),
        "num_trades": len(trades),
        "win_rate": round(len(wins) / len(rets) * 100, 1) if len(rets) else None,
        "avg_win_pct": round(float(wins.mean()) * 100, 2) if len(wins) else None,
        "avg_loss_pct": round(float(losses.mean()) * 100, 2) if len(losses) else None,
        "profit_factor": (round(float(wins.sum() / -losses.sum()), 2)
                          if len(losses) and losses.sum() != 0 else None),
        "expectancy_pct": round(float(rets.mean()) * 100, 2) if len(rets) else None,
        "total_return_pct": round((eq[-1] - 1) * 100, 2),
        "option_proxy_return_pct": round((opt_equity[-1] - 1) * 100, 2),
        "buy_hold_return_pct": round(buy_hold, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "equity_curve": [round(x, 4) for x in equity],
        "trades": trades[-100:],
        "score_now": float(scores.iloc[-1]),
        "note": ("Backtest uses the same composite signal shown live. Option-proxy return "
                 "approximates a ~0.35-delta long option at 5x effective leverage; real option "
                 "P&L also depends on IV changes and theta. Past performance never guarantees "
                 "future results."),
    }
