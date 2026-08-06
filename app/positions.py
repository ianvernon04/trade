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


def _trade_rows_from_tracking() -> list[dict]:
    """Reconstruct open positions from the newest ingested broker snapshot.

    The Position and Risk Managers used to read only `journal.trades` — the
    human's hand-kept log. An account traded through Robinhood never appears
    there, so both agents reported "0 positions / LOW risk" while real money
    was at risk. That is the dangerous direction for a risk gauge: silence
    reads as safety.

    `python3 -m app ingest` already stores broker positions as `position`
    events with the raw payload preserved, so the snapshot is here — it just
    was not wired up. Events are newest-first, so the first row seen for a
    contract wins and older snapshots of the same contract are skipped.

    Quantity 0 means closed: brokers keep returning closed contracts for a
    while, and counting them would resurrect positions that no longer exist.
    """
    from . import tracking

    events = [e for e in tracking.list_events(kind="position", limit=5000)
              if isinstance(e.get("payload"), dict)]

    # Snapshot semantics, not per-contract merge. Merging by contract cannot
    # express "this position is gone": a closed contract often disappears from
    # the broker's list entirely, or comes back stripped of strike/expiry, so
    # there is no newer row to overwrite the old one and a dead position lives
    # forever. Taking only the newest batch per asset class means anything
    # absent from that batch is correctly treated as closed.
    #
    # Options and equities are tracked separately because they arrive from
    # different calls and one is often re-ingested without the other.
    def _cls(p: dict) -> str:
        return "option" if (p.get("option_type") or p.get("strike_price")
                            or p.get("expiration_date")) else "stock"

    def _ts(e: dict) -> datetime | None:
        try:
            return datetime.fromisoformat(str(e["ts"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            return None

    # A single ingest writes its rows over a second or two, so the batch is a
    # short window around the newest event rather than one exact timestamp.
    BATCH_WINDOW_S = 120
    newest: dict[str, datetime] = {}
    for e in events:                                   # newest-first
        c = _cls(e["payload"])
        t = _ts(e)
        if t and c not in newest:
            newest[c] = t

    latest: dict[tuple, dict] = {}
    for e in events:
        p = e["payload"]
        c, t = _cls(p), _ts(e)
        if t is None or c not in newest:
            continue
        if (newest[c] - t).total_seconds() > BATCH_WINDOW_S:
            continue                                   # older snapshot, ignore
        symbol = str(p.get("symbol") or p.get("chain_symbol") or e.get("ticker") or "").upper()
        if not symbol:
            continue
        opt = str(p.get("option_type") or "").lower()
        key = (symbol, opt, str(p.get("strike_price") or ""), str(p.get("expiration_date") or ""))
        latest.setdefault(key, p)

    rows = []
    for (symbol, opt, strike, expiry), p in latest.items():
        try:
            qty = float(p.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue

        instrument = opt if opt in ("call", "put") else "stock"
        # Option average_price is per contract; journal entry_price is per
        # share. Dividing by the multiplier keeps both on the same scale, so
        # the shared P&L maths below stays correct for either source.
        if instrument == "stock":
            entry = p.get("average_buy_price") or p.get("average_price")
            mult = 1.0
        else:
            entry = p.get("average_price")
            try:
                mult = float(p.get("trade_value_multiplier") or 100) or 100.0
            except (TypeError, ValueError):
                mult = 100.0
        try:
            entry_price = float(entry) / mult if entry is not None else None
        except (TypeError, ValueError):
            entry_price = None
        if entry_price is None:
            continue

        rows.append({
            "id": f"rh:{symbol}:{instrument}:{strike or '-'}:{expiry or '-'}",
            "ticker": symbol,
            "instrument": instrument,
            "direction": "short" if str(p.get("type") or "long").lower() == "short" else "long",
            "quantity": abs(qty),
            "strike": float(strike) if strike else None,
            "expiry": expiry or None,
            "entry_price": entry_price,
            "entry_date": str(p.get("opened_at") or "")[:10] or None,
        })
    return rows


STALE_AFTER_HOURS = 12


def broker_positions() -> dict:
    """Live-marked positions from the latest ingested broker snapshot.

    Marks are live, but the *set of positions* is only as fresh as the last
    ingest. A contract closed since then still appears here. The staleness is
    reported rather than hidden: an agent that silently trusts an old snapshot
    is worse than one that says it does not know.
    """
    from . import tracking

    rows = _trade_rows_from_tracking()
    out = _mark_trades(rows)
    out["source"] = "tracking-store (ingested broker snapshot)"

    events = tracking.list_events(kind="position", limit=1)
    as_of = events[0]["ts"] if events else None
    out["as_of"] = as_of
    out["stale"] = False
    if as_of:
        try:
            seen = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - seen).total_seconds() / 3600
            out["age_hours"] = round(age_h, 1)
            if age_h > STALE_AFTER_HOURS:
                out["stale"] = True
                out["warnings"].insert(0, (
                    f"snapshot is {age_h:.0f}h old — positions closed since then still "
                    "appear, and new ones are missing. Re-run `ingest` before trusting this."))
        except ValueError:
            pass
    elif rows:
        out["warnings"].insert(0, "snapshot age unknown")
    return out


def live_positions(user_id: int) -> dict:
    """Live-marked positions from the human's journal."""
    out = _mark_trades(journal.list_trades(user_id, "open"))
    out["source"] = "journal"
    return out


def _mark_trades(trade_rows: list[dict]) -> dict:
    """Mark a batch of position rows to market and roll up portfolio greeks.

    Shared by both sources so the journal and the broker snapshot are valued
    by identical logic — a divergence here would make the two disagree about
    the same position.
    """
    positions = []
    totals = {"cost_basis": 0.0, "market_value": 0.0, "unrealized_pnl": 0.0,
              "delta_shares": 0.0, "theta_per_day": 0.0}
    warnings = []
    today = date.today()

    for t in trade_rows:
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
