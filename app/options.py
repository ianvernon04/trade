"""Options chain analysis: greeks, chain-wide sentiment metrics, and a
call/put recommendation that combines the stock signal with what the
options market itself is pricing in.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import data
from .signals import score_frame

RISK_FREE = 0.045  # approximate T-bill yield used for greeks


# ---------- Black-Scholes ----------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs_greeks(S: float, K: float, T: float, sigma: float, kind: str, r: float = RISK_FREE) -> dict:
    """Black-Scholes greeks for one contract. T in years, sigma as decimal."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    gamma = _norm_pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * _norm_pdf(d1) * math.sqrt(T) / 100  # per 1 vol point
    if kind == "call":
        delta = _norm_cdf(d1)
        theta = (-S * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-S * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 5),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
    }


# ---------- Chain analysis ----------

def _years_to_expiry(expiry: str) -> float:
    exp = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=21, tzinfo=timezone.utc)
    return max((exp - datetime.now(timezone.utc)).total_seconds() / (365 * 24 * 3600), 1e-6)


def _max_pain(calls: pd.DataFrame, puts: pd.DataFrame) -> float | None:
    """Strike where total intrinsic value paid out to option holders is minimized."""
    strikes = sorted(set(calls["strike"]).union(puts["strike"]))
    if not strikes:
        return None
    c = calls.set_index("strike")["openInterest"].fillna(0)
    p = puts.set_index("strike")["openInterest"].fillna(0)
    best, best_pay = None, float("inf")
    for s in strikes:
        call_pay = sum(oi * max(0, s - k) for k, oi in c.items())
        put_pay = sum(oi * max(0, k - s) for k, oi in p.items())
        if call_pay + put_pay < best_pay:
            best_pay, best = call_pay + put_pay, s
    return float(best) if best is not None else None


def _slim(df: pd.DataFrame, spot: float, T: float, kind: str) -> list[dict]:
    """Trim a chain side to the columns the UI needs, with computed greeks."""
    keep = df[
        (df["strike"] > spot * 0.75) & (df["strike"] < spot * 1.25)
    ].copy() if len(df) > 40 else df.copy()
    rows = []
    for _, r in keep.iterrows():
        iv = float(r.get("impliedVolatility") or 0)
        greeks = bs_greeks(spot, float(r["strike"]), T, iv, kind)
        rows.append({
            "strike": float(r["strike"]),
            "last": _f(r.get("lastPrice")),
            "bid": _f(r.get("bid")),
            "ask": _f(r.get("ask")),
            "volume": _i(r.get("volume")),
            "open_interest": _i(r.get("openInterest")),
            "iv": round(iv * 100, 1) if iv else None,
            "in_the_money": bool(r.get("inTheMoney", False)),
            **greeks,
        })
    return rows


def analyze_chain(ticker: str, expiry: str | None = None) -> dict:
    """Full chain analysis + call/put recommendation for one expiry."""
    quote = data.get_quote(ticker)
    spot = quote["price"]
    chain = data.get_option_chain(ticker, expiry)
    calls, puts = chain["calls"], chain["puts"]
    expiry = chain["expiry"]
    T = _years_to_expiry(expiry)
    dte = round(T * 365)

    call_vol = int(calls["volume"].fillna(0).sum())
    put_vol = int(puts["volume"].fillna(0).sum())
    call_oi = int(calls["openInterest"].fillna(0).sum())
    put_oi = int(puts["openInterest"].fillna(0).sum())
    pcr_vol = round(put_vol / call_vol, 3) if call_vol else None
    pcr_oi = round(put_oi / call_oi, 3) if call_oi else None

    # ATM implied volatility and the expected move it prices in.
    atm_call = calls.iloc[(calls["strike"] - spot).abs().argsort()[:2]]
    atm_put = puts.iloc[(puts["strike"] - spot).abs().argsort()[:2]]
    ivs = pd.concat([atm_call["impliedVolatility"], atm_put["impliedVolatility"]]).dropna()
    atm_iv = float(ivs.mean()) if len(ivs) else None
    expected_move = round(spot * atm_iv * math.sqrt(T), 2) if atm_iv else None

    # IV skew: 25-delta-ish put IV minus call IV (crash-fear gauge).
    otm_puts = puts[puts["strike"] < spot * 0.97]
    otm_calls = calls[calls["strike"] > spot * 1.03]
    skew = None
    if len(otm_puts) and len(otm_calls):
        skew = round(
            (float(otm_puts["impliedVolatility"].mean())
             - float(otm_calls["impliedVolatility"].mean())) * 100, 1)

    # Unusual activity: volume far above open interest.
    unusual = []
    for kind, side in (("call", calls), ("put", puts)):
        s = side[(side["volume"].fillna(0) > 500)
                 & (side["volume"].fillna(0) > 2 * side["openInterest"].fillna(0))]
        for _, r in s.iterrows():
            unusual.append({
                "type": kind, "strike": float(r["strike"]),
                "volume": _i(r["volume"]), "open_interest": _i(r["openInterest"]),
                "last": _f(r["lastPrice"]),
            })
    unusual.sort(key=lambda x: -(x["volume"] or 0))

    # Stock-side signal feeds the direction call.
    hist = data.get_history(ticker, "1y", "1d")
    stock = score_frame(hist)

    rec = _recommend(ticker, spot, stock, pcr_vol, skew, atm_iv, expiry, dte, calls, puts, T)

    return {
        "ticker": ticker.upper(),
        "spot": spot,
        "expiry": expiry,
        "dte": dte,
        "expiries": data.get_option_expiries(ticker),
        "put_call_ratio_volume": pcr_vol,
        "put_call_ratio_oi": pcr_oi,
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "atm_iv": round(atm_iv * 100, 1) if atm_iv else None,
        "expected_move": expected_move,
        "expected_move_pct": round(expected_move / spot * 100, 2) if expected_move else None,
        "iv_skew": skew,
        "max_pain": _max_pain(calls, puts),
        "unusual_activity": unusual[:10],
        "stock_signal": stock,
        "recommendation": rec,
        "calls": _slim(calls, spot, T, "call"),
        "puts": _slim(puts, spot, T, "put"),
    }


def _recommend(ticker, spot, stock, pcr_vol, skew, atm_iv, expiry, dte,
               calls, puts, T) -> dict:
    """Combine stock signal + options sentiment into a call/put suggestion."""
    score = stock["score"]
    reasons = []

    # Options-market adjustments on top of the stock score.
    adj = 0.0
    if pcr_vol is not None:
        if pcr_vol > 1.2:
            adj -= 5
            reasons.append(f"Put/call volume ratio {pcr_vol} — options flow leaning bearish")
        elif pcr_vol < 0.7:
            adj += 5
            reasons.append(f"Put/call volume ratio {pcr_vol} — options flow leaning bullish")
        else:
            reasons.append(f"Put/call volume ratio {pcr_vol} — balanced flow")
    if skew is not None and abs(skew) > 5:
        adj += -3 if skew > 0 else 3
        reasons.append(f"IV skew {skew:+.1f} pts — {'downside protection bid up' if skew > 0 else 'upside calls bid up'}")

    combined = max(-100, min(100, score + adj))

    if combined >= 15:
        direction, side = "bullish", "call"
    elif combined <= -15:
        direction, side = "bearish", "put"
    else:
        direction, side = "neutral", None

    reasons.insert(0, f"Stock technical score {score:+.1f} ({stock['label']})")

    suggestion = None
    if side:
        # Target ~0.35 delta: meaningful participation without pure lottery pricing.
        table = calls if side == "call" else puts
        best, best_d = None, 1e9
        for _, r in table.iterrows():
            iv = float(r.get("impliedVolatility") or 0)
            g = bs_greeks(spot, float(r["strike"]), T, iv, side)
            if g["delta"] is None:
                continue
            dist = abs(abs(g["delta"]) - 0.35)
            if dist < best_d:
                best_d, best = dist, (r, g)
        if best is not None:
            r, g = best
            mid = None
            if _f(r.get("bid")) and _f(r.get("ask")):
                mid = round((float(r["bid"]) + float(r["ask"])) / 2, 2)
            suggestion = {
                "action": f"BUY {side.upper()}",
                "strike": float(r["strike"]),
                "expiry": expiry,
                "dte": dte,
                "est_mid_price": mid or _f(r.get("lastPrice")),
                "delta": g["delta"],
                "theta": g["theta"],
                "iv": round(float(r.get("impliedVolatility") or 0) * 100, 1),
                "note": ("~0.35-delta contract: balances directional exposure vs premium risk. "
                         "Prefer 30-45 DTE to reduce theta burn."
                         + (" WARNING: this expiry is under 21 DTE — theta decay accelerates."
                            if dte < 21 else "")),
            }
    if atm_iv and atm_iv > 0.6:
        reasons.append(f"ATM IV {atm_iv * 100:.0f}% is elevated — options are expensive; "
                       "consider spreads instead of naked long options")

    return {
        "combined_score": round(combined, 1),
        "direction": direction,
        "bias": ("CALL" if side == "call" else "PUT" if side == "put" else "NO TRADE"),
        "reasons": reasons,
        "suggestion": suggestion,
    }


def _f(x):
    try:
        v = float(x)
        return round(v, 4) if not math.isnan(v) else None
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        v = float(x)
        return int(v) if not math.isnan(v) else 0
    except (TypeError, ValueError):
        return 0
