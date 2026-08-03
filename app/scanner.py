"""Morning scanner: sweep the watchlist and rank today's best setups."""

from __future__ import annotations

import threading
import time

from . import data
from .scorecard import confluence, weekly_signal
from .signals import score_frame, score_series

_cache: dict = {}
_lock = threading.Lock()
SCAN_TTL = 600  # seconds


def scan(tickers: list[str] | None = None, force: bool = False) -> dict:
    key = ",".join(sorted(tickers)) if tickers else "default"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < SCAN_TTL and not force:
            return hit[1]

    setups = []
    for t in (tickers or data.DEFAULT_WATCHLIST):
        try:
            df = data.get_history(t, "1y", "1d")
            sig = score_frame(df)
            scores = score_series(df)
            s_now = float(scores.iloc[-1])
            s_prev = float(scores.iloc[-2]) if len(scores) > 1 else s_now
            crossed_buy = s_now >= 20 and s_prev < 20
            crossed_sell = s_now <= -15 and s_prev > -15
            try:
                conf = confluence(sig, weekly_signal(t))
            except Exception:
                conf = {"state": "unknown", "weekly_label": "N/A", "weekly_score": None,
                        "note": "weekly data unavailable"}
            earnings = data.get_earnings_info(t)

            # Rank: signal strength, fresh crossings and timeframe agreement
            # score highest; imminent earnings drags a setup down.
            rank = abs(s_now)
            if crossed_buy or crossed_sell:
                rank += 15
            if conf["state"] == "agree":
                rank += 10
            elif conf["state"] == "conflict":
                rank -= 10
            edays = earnings.get("days_until")
            if edays is not None and edays <= 7:
                rank -= 8

            setups.append({
                "ticker": t,
                "close": sig["close"],
                "score": s_now,
                "label": sig["label"],
                "crossed_buy": crossed_buy,
                "crossed_sell": crossed_sell,
                "weekly_label": conf["weekly_label"],
                "confluence": conf["state"],
                "earnings_in_days": edays,
                "rsi": sig["rsi"],
                "rank": round(rank, 1),
            })
        except Exception as e:
            setups.append({"ticker": t, "error": str(e)})

    ok = [s for s in setups if "error" not in s]
    ok.sort(key=lambda x: -x["rank"])
    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "setups": ok + [s for s in setups if "error" in s],
        "note": ("Ranked by signal strength, fresh threshold crossings, and daily/weekly "
                 "agreement; imminent earnings lowers the rank. Top rows are today's most "
                 "interesting setups, not guaranteed winners."),
    }
    with _lock:
        _cache[key] = (time.time(), result)
    return result
