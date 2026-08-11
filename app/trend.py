"""Daily trend read for the tickers this account actually trades.

The scanner answers "what looks interesting today". This answers a different
question: for the things already held or traded, which way are they going and
how convincingly. Those need different numbers — a fresh crossover matters for
an entry, whereas for a position you care about direction, persistence, and
whether the short and medium horizons still agree.

**The universe is resolved at runtime from local data, never hardcoded.** The
holdings live in journal.db, which is gitignored, while this app deploys
publicly from GitHub. A literal list of tickers in this file would put the
owner's book on the internet — the exact leak that had to be scrubbed out of
git history on 2026-08-06. So `universe()` reads the tracking store, and where
there is no tracking store (the public deployment) it falls back to the
generic watchlist and says so in `source`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import data, indicators, tracking

# Trend classification in ATR-per-day units, so a $500 stock and a $15 stock
# are measured on the same scale — a dollar of drift means very different
# things for each, but an ATR does not.
FLAT_BAND = 0.04      # |slope| below this is drift, not direction
STRONG_BAND = 0.15    # above this the move is doing real work

LOOKBACK = 20         # trading days the trend is measured over
CACHE_TTL = 300       # seconds; this is a monitoring view, not a tape


def universe() -> dict:
    """Tickers this account trades, newest evidence first.

    Three local sources, in descending order of "actually mine":
    currently held → decided on → previously held. Falls back to the default
    watchlist when the tracking store is empty, which is what the public
    deployment sees.
    """
    held: list[str] = []
    decided: list[str] = []
    past: list[str] = []

    try:
        from . import portfolio
        snap = portfolio.current(enrich_rows=False)
        held = sorted({p["ticker"] for p in snap.get("positions") or [] if p.get("ticker")})
    except Exception:
        pass

    try:
        decided = sorted({d["ticker"] for d in tracking.list_decisions(limit=500)
                          if d.get("ticker")})
    except Exception:
        pass

    try:
        past = sorted({e["ticker"] for e in tracking.list_events(kind="position", limit=1000)
                       if e.get("ticker")})
    except Exception:
        pass

    seen: set[str] = set()
    ordered: list[str] = []
    origin: dict[str, str] = {}
    for group, label in ((held, "held"), (decided, "decided"), (past, "past")):
        for t in group:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
                origin[t] = label

    if ordered:
        return {"tickers": ordered, "origin": origin,
                "source": "account", "held": held}

    return {"tickers": list(data.DEFAULT_WATCHLIST), "origin": {},
            "source": "watchlist", "held": []}


def _streak(closes: pd.Series) -> int:
    """Consecutive daily closes in one direction. Negative when falling."""
    diffs = closes.diff().dropna()
    if diffs.empty:
        return 0
    sign = np.sign(diffs.iloc[-1])
    if sign == 0:
        return 0
    n = 0
    for d in reversed(diffs.tolist()):
        if np.sign(d) != sign:
            break
        n += 1
    return int(n * sign)


def _pct(a: float, b: float):
    """Percent change b→a, or None when the base is unusable."""
    if b in (0, None) or a is None or not np.isfinite(a) or not np.isfinite(b):
        return None
    return round((a / b - 1) * 100, 2)


def daily_trend(ticker: str) -> dict:
    """One ticker's trend read. Never raises — errors ride in the payload."""
    try:
        df = data.get_history(ticker, period="6mo", interval="1d")
    except Exception as exc:  # noqa: BLE001 — one bad ticker must not kill the tab
        return {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}

    if df is None or len(df) < LOOKBACK + 5:
        return {"ticker": ticker, "error": "not enough history"}

    close = df["close"].astype(float)
    last = float(close.iloc[-1])

    # Slope of the last LOOKBACK closes, expressed in ATR per day. Regression
    # rather than endpoint-to-endpoint so one gap day cannot define the trend.
    window = close.iloc[-LOOKBACK:]
    x = np.arange(len(window), dtype=float)
    slope_per_day = float(np.polyfit(x, window.values.astype(float), 1)[0])
    atr = float(indicators.atr(df, 14).iloc[-1])
    slope_atr = round(slope_per_day / atr, 4) if atr and np.isfinite(atr) and atr > 0 else 0.0

    # Two independent readings of the same window: the regression slope, and
    # where the price actually ended up. They usually agree. When they don't —
    # a slide early in the window that later recovered can leave a rising fit
    # over a net loss — neither one is the trend, and calling it "up" because
    # the fit says so would contradict the returns printed beside it. That
    # disagreement is the finding, so it is reported rather than resolved.
    net_move = _pct(last, float(close.iloc[-(LOOKBACK + 1)])) if len(close) > LOOKBACK else None
    conflict = (net_move is not None
                and abs(slope_atr) > FLAT_BAND
                and abs(net_move) > 1.0
                and np.sign(slope_atr) != np.sign(net_move))

    if conflict:
        direction = "sideways"
    elif slope_atr > FLAT_BAND:
        direction = "up"
    elif slope_atr < -FLAT_BAND:
        direction = "down"
    else:
        direction = "sideways"

    mag = abs(slope_atr)
    strength = ("weak" if conflict
                else "strong" if mag >= STRONG_BAND
                else "moderate" if mag >= FLAT_BAND
                else "weak")

    sma20 = float(indicators.sma(close, 20).iloc[-1])
    sma50 = float(indicators.sma(close, 50).iloc[-1]) if len(close) >= 50 else float("nan")
    above20 = bool(last > sma20) if np.isfinite(sma20) else None
    above50 = bool(last > sma50) if np.isfinite(sma50) else None
    stack = None
    if np.isfinite(sma20) and np.isfinite(sma50):
        stack = "bullish" if sma20 > sma50 else "bearish"

    # How much of the move was one-way. A trend that only rose on 11 of 20
    # days is a different animal from one that rose on 17, even at equal slope.
    ups = int((window.diff().dropna() > 0).sum())
    total = int(len(window.diff().dropna()))
    consistency = round(100 * ups / total) if total else None

    return {
        "ticker": ticker,
        "price": round(last, 2),
        "change_1d": _pct(last, float(close.iloc[-2])),
        "change_5d": _pct(last, float(close.iloc[-6])) if len(close) > 6 else None,
        "change_20d": _pct(last, float(close.iloc[-21])) if len(close) > 21 else None,
        "direction": direction,
        "strength": strength,
        "conflict": bool(conflict),
        "slope_atr": slope_atr,
        "streak": _streak(close),
        "consistency": consistency,
        "above_sma20": above20,
        "above_sma50": above50,
        "ma_stack": stack,
        "lookback_days": LOOKBACK,
    }


def _summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    if not ok:
        return {"up": 0, "down": 0, "sideways": 0, "tone": "unknown", "n": 0}
    up = sum(1 for r in ok if r["direction"] == "up")
    down = sum(1 for r in ok if r["direction"] == "down")
    side = len(ok) - up - down
    if up > down * 2:
        tone = "broadly rising"
    elif down > up * 2:
        tone = "broadly falling"
    elif up or down:
        tone = "mixed"
    else:
        tone = "flat"
    return {"up": up, "down": down, "sideways": side, "tone": tone, "n": len(ok)}


def report(tickers: list[str] | None = None) -> dict:
    """Trend across the account's tickers, strongest move first."""
    uni = universe()
    syms = tickers or uni["tickers"]

    def build() -> dict:
        rows = [daily_trend(t) for t in syms]
        rows.sort(key=lambda r: (r.get("error") is not None,
                                 -abs(r.get("slope_atr") or 0)))
        for r in rows:
            r["origin"] = uni["origin"].get(r["ticker"], "watchlist")
        return {
            "generated_at": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "source": uni["source"],
            "lookback_days": LOOKBACK,
            "summary": _summarize(rows),
            "trends": rows,
        }

    key = "trend:" + ",".join(sorted(syms))
    return data._cached(key, CACHE_TTL, build)
