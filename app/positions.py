"""Live open-position tracking: mark journal positions to market and roll
up portfolio-level greeks so the trader can see risk, not just history."""

from __future__ import annotations

from datetime import date, datetime, timezone

from . import data, journal
from .options import bs_greeks, _f


def _mark_option(trade: dict) -> dict | None:
    """Find the live contract matching an option trade; None if unavailable."""
    if trade["instrument"] not in ("call", "put") or not trade.get("strike") or not trade.get("expiry"):
        return None
    try:
        chain = data.get_option_chain(trade["ticker"], trade["expiry"])
        if chain["expiry"] != trade["expiry"]:
            return None  # expiry no longer listed (already expired)
        side = chain["calls"] if trade["instrument"] == "call" else chain["puts"]
        row = side[side["strike"] == float(trade["strike"])]
        if row.empty:
            return None
        r = row.iloc[0]
        bid, ask, last = _f(r.get("bid")), _f(r.get("ask")), _f(r.get("lastPrice"))
        mark = round((bid + ask) / 2, 3) if bid and ask else last
        spot = data.get_quote(trade["ticker"])["price"]
        exp = datetime.strptime(trade["expiry"], "%Y-%m-%d").replace(hour=21, tzinfo=timezone.utc)
        T = max((exp - datetime.now(timezone.utc)).total_seconds() / (365 * 24 * 3600), 1e-6)
        iv = float(r.get("impliedVolatility") or 0)
        greeks = bs_greeks(spot, float(trade["strike"]), T, iv, trade["instrument"])
        return {"mark": mark, "iv": round(iv * 100, 1) if iv else None, **greeks}
    except Exception:
        return None


def live_positions(user_id: int) -> dict:
    positions = []
    totals = {"cost_basis": 0.0, "market_value": 0.0, "unrealized_pnl": 0.0,
              "delta_shares": 0.0, "theta_per_day": 0.0}
    warnings = []
    today = date.today()

    for t in journal.list_trades(user_id, "open"):
        sign = 1 if t["direction"] == "long" else -1
        mult = journal.MULT.get(t["instrument"], 100)
        entry_cost = t["entry_price"] * t["quantity"] * mult
        pos = {
            "id": t["id"], "ticker": t["ticker"], "instrument": t["instrument"],
            "direction": t["direction"], "quantity": t["quantity"],
            "strike": t["strike"], "expiry": t["expiry"],
            "entry_price": t["entry_price"], "entry_date": t["entry_date"],
            "mark": None, "unrealized_pnl": None, "unrealized_pnl_pct": None,
            "delta_shares": None, "theta_per_day": None, "iv": None,
            "dte": None, "earnings_in_days": None, "flags": [],
        }
        try:
            quote = data.get_quote(t["ticker"])
            pos["underlying_price"] = quote["price"]
        except Exception:
            pos["underlying_price"] = None

        if t["instrument"] == "stock":
            mark = pos["underlying_price"]
            if mark is not None:
                pos["mark"] = mark
                pos["delta_shares"] = round(sign * t["quantity"], 2)
                pos["theta_per_day"] = 0.0
        else:
            m = _mark_option(t)
            if m and m["mark"] is not None:
                pos["mark"] = m["mark"]
                pos["iv"] = m["iv"]
                if m["delta"] is not None:
                    pos["delta_shares"] = round(sign * m["delta"] * t["quantity"] * 100, 1)
                if m["theta"] is not None:
                    pos["theta_per_day"] = round(sign * m["theta"] * t["quantity"] * 100, 2)
            if t.get("expiry"):
                try:
                    pos["dte"] = (date.fromisoformat(t["expiry"]) - today).days
                except ValueError:
                    pass

        if pos["mark"] is not None:
            value = pos["mark"] * t["quantity"] * mult
            pnl = round(sign * (value - entry_cost), 2)
            pos["unrealized_pnl"] = pnl
            pos["unrealized_pnl_pct"] = round(pnl / entry_cost * 100, 1) if entry_cost else None
            totals["market_value"] += value
            totals["unrealized_pnl"] += pnl
        totals["cost_basis"] += entry_cost
        if pos["delta_shares"] is not None:
            totals["delta_shares"] += pos["delta_shares"]
        if pos["theta_per_day"] is not None:
            totals["theta_per_day"] += pos["theta_per_day"]

        try:
            e = data.get_earnings_info(t["ticker"])
            pos["earnings_in_days"] = e.get("days_until")
        except Exception:
            pass

        if pos["dte"] is not None and pos["dte"] <= 7:
            pos["flags"].append(f"expires in {pos['dte']}d")
        if pos["earnings_in_days"] is not None and pos["earnings_in_days"] <= 5:
            pos["flags"].append(f"earnings in {pos['earnings_in_days']}d")
        if pos["unrealized_pnl_pct"] is not None:
            if pos["unrealized_pnl_pct"] <= -50:
                pos["flags"].append("down >50% — cut or accept the loss consciously")
            elif pos["unrealized_pnl_pct"] >= 100:
                pos["flags"].append("up >100% — take partial profits")
        warnings.extend(f"{t['ticker']}: {f}" for f in pos["flags"])
        positions.append(pos)

    for k in totals:
        totals[k] = round(totals[k], 2)
    return {
        "positions": positions,
        "totals": totals,
        "warnings": warnings,
        "note": ("Marks use live option mid-prices (or last) from Yahoo; greeks are Black-Scholes. "
                 "delta_shares = share-equivalent exposure; theta_per_day = expected daily P&L "
                 "from time decay at current IV."),
    }
