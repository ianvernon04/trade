"""The Position Manager: agent №3, who watches open positions for exits and rolls.

Continuously scans open positions per user, monitors Greeks decay, earnings/expiry
warnings, and thesis breakdowns (reversal of signal). Findings travel to the Trader
over the shared message bus as alerts and briefings. The Position Manager logs its
surveillance but never calls broker tools — the Trader decides when to close/roll.

Holdings come from the broker snapshot the Trader ingests (app/portfolio.py),
not from a hand-kept journal — so this agent is useful to someone who never
opens the Journal tab, and it says "blind" rather than "no positions" when
nobody has given it a snapshot to read.

Design notes:
- analyze_positions() is a pure function so tests can inject fixture positions.
- run_scan() takes an injectable `book` for the same reason.
- Background loop checks every 30 min (longer than Analyst's 15 min, since
  position risk moves slower than headlines).
"""

from __future__ import annotations

import threading
import time

from . import tracking

AGENT_ID = "position_manager"
TRADER_ID = "trader"
CHECK_INTERVAL = 1800  # seconds (30 min)

# Alerts when theta bleed is steep or position is near the edge.
THETA_ALERT_PER_DAY_USD = 50  # theta decay >$50/day warrants a close-soon alert
DTE_ALERT = 7  # days to expiry for warning
EARNINGS_ALERT = 5  # days to earnings for warning


def analyze_positions(pos_list: list[dict]) -> dict:
    """Analyze a batch of open positions for exit opportunities and risk."""
    actions = []  # exit, roll, take_profit, theta_decay, earnings_risk, reversal
    theta_sum = 0.0
    dte_warns = []
    earnings_warns = []

    # A position whose chain wouldn't load has real exposure and no greeks.
    # It is excluded from theta_sum, so it must be counted and reported
    # rather than quietly rounding the portfolio's decay down toward zero.
    unpriced = [p for p in pos_list if p.get("theta_per_day") is None]

    for p in pos_list:
        alerts = []
        specific_action = None  # take_profit, reversal, or None

        # Theta decay alert.
        if p.get("theta_per_day") is not None and abs(p["theta_per_day"]) >= THETA_ALERT_PER_DAY_USD:
            alerts.append(f"steep theta: ${p['theta_per_day']:.0f}/day")
            theta_sum += abs(p["theta_per_day"])

        # Profitability: take profits at 50%+.
        if p.get("unrealized_pnl_pct") is not None and p["unrealized_pnl_pct"] >= 50:
            alerts.append(f"up {p['unrealized_pnl_pct']:.0f}% — consider taking profit")
            specific_action = "take_profit"
            actions.append({
                "ticker": p["ticker"],
                "type": "take_profit",
                "position_id": p["id"],
                "pnl_pct": p["unrealized_pnl_pct"],
                "note": f"{p['instrument']} @ {p['strike']} is up {p['unrealized_pnl_pct']:.0f}%",
            })

        # Losses >50%: cut or accept consciously.
        if p.get("unrealized_pnl_pct") is not None and p["unrealized_pnl_pct"] <= -50:
            alerts.append(f"down {abs(p['unrealized_pnl_pct']):.0f}% — thesis broken?")
            specific_action = "reversal"
            actions.append({
                "ticker": p["ticker"],
                "type": "reversal",
                "position_id": p["id"],
                "pnl_pct": p["unrealized_pnl_pct"],
                "note": f"{p['instrument']} at {p['strike']} down {abs(p['unrealized_pnl_pct']):.0f}%",
            })

        # Expiry warning.
        if p.get("dte") is not None and 0 <= p["dte"] <= DTE_ALERT:
            alerts.append(f"expires in {p['dte']}d")
            dte_warns.append((p["ticker"], p["dte"], p))

        # Earnings warning.
        if p.get("earnings_in_days") is not None and 0 < p["earnings_in_days"] <= EARNINGS_ALERT:
            alerts.append(f"earnings {p['earnings_in_days']}d away")
            earnings_warns.append((p["ticker"], p["earnings_in_days"], p))

        # Add generic monitoring action only if there are alerts and no specific action.
        if alerts and not specific_action:
            actions.append({
                "ticker": p["ticker"],
                "instrument": p["instrument"],
                "position_id": p["id"],
                "strike": p.get("strike"),
                "expiry": p.get("expiry"),
                "unrealized_pnl_pct": p.get("unrealized_pnl_pct"),
                "alerts": alerts,
            })

    return {
        "n_positions": len(pos_list),
        "theta_sum": round(theta_sum, 2),
        "unpriced": len(unpriced),
        "unpriced_tickers": sorted({p.get("ticker") for p in unpriced if p.get("ticker")}),
        "actions": actions,
        "dte_warns": dte_warns,
        "earnings_warns": earnings_warns,
    }


def compose_messages(a: dict, user_id: int | None = None) -> list[dict]:
    """Turn position analysis into inbox-ready messages for the Trader."""
    msgs = []

    # Not seeing the account is the one finding that outranks everything
    # else, and it must never be delivered as a calm "no positions".
    if a.get("blind"):
        unreadable = a.get("unreadable") or 0
        return [{
            "kind": "alert", "priority": "high",
            "subject": "Position Manager is blind — no broker snapshot",
            "body": ("I cannot see the account: no readable Robinhood position "
                     "data has been ingested"
                     + (f" ({unreadable} position event(s) had no payload to read)"
                        if unreadable else "")
                     + ". Treat every position read as unknown until you pull "
                       "positions from the Robinhood MCP server and record them "
                       "with `python3 -m app ingest --payload -`. Until then no "
                       "exit, roll, or expiry warning below is trustworthy."),
            "payload": {"blind": True, "unreadable": unreadable},
            "dedupe_hours": 6,
        }]

    if a.get("stale"):
        msgs.append({
            "kind": "alert", "priority": "high",
            "subject": "Broker snapshot is stale",
            "body": (f"The newest position data is {a.get('age_hours')}h old "
                     f"(as of {a.get('as_of')}). Everything below describes the "
                     "book as it was then — re-pull positions from the Robinhood "
                     "MCP server and re-ingest before acting on it."),
            "payload": {"age_hours": a.get("age_hours"), "as_of": a.get("as_of")},
            "dedupe_hours": 6,
        })

    for act in a["actions"]:
        if act["type"] == "take_profit":
            msgs.append({
                "kind": "briefing", "priority": "normal", "ticker": act["ticker"],
                "subject": f"Position P&L: {act['ticker']} up {act['pnl_pct']:.0f}%",
                "body": (f"{act['note']}. Consider locking in the win before expiry/earnings decay it away."),
                "payload": act, "dedupe_hours": 6,
            })
        elif act["type"] == "reversal":
            msgs.append({
                "kind": "alert", "priority": "high", "ticker": act["ticker"],
                "subject": f"Position reversal risk: {act['ticker']} {act['pnl_pct']:.0f}%",
                "body": (f"{act['note']}. Original thesis may be broken. "
                         "Review the signal and decide: hold if thesis intact, or cut the loss."),
                "payload": act, "dedupe_hours": 24,
            })

    # Aggregate expiry warnings.
    if a["dte_warns"]:
        dte_list = ", ".join(f"{t} in {d}d" for t, d, _ in a["dte_warns"])
        msgs.append({
            "kind": "briefing", "priority": "normal",
            "subject": "Position expiry approaching",
            "body": f"Theta decay is steepest in the final week: {dte_list}. "
                    "Decide: close for a clean exit, or roll to a later date/strike.",
            "payload": {"dte_warns": a["dte_warns"]}, "dedupe_hours": 12,
        })

    # Aggregate earnings warnings.
    if a["earnings_warns"]:
        earn_list = ", ".join(f"{t} in {d}d" for t, d, _ in a["earnings_warns"])
        msgs.append({
            "kind": "alert", "priority": "high",
            "subject": "Earnings before expiry — IV crush risk",
            "body": f"Earnings dates landing before your option expirations: {earn_list}. "
                    "IV crush will erode your position after earnings. "
                    "Close before the report unless earnings is your thesis.",
            "payload": {"earnings_warns": a["earnings_warns"]}, "dedupe_hours": 24,
        })

    # Positions we hold but couldn't price: exposure without a read on it.
    if a.get("unpriced"):
        tickers = a.get("unpriced_tickers") or []
        msgs.append({
            "kind": "alert", "priority": "high",
            "subject": "Positions could not be priced",
            "body": (f"{a['unpriced']} of {a['n_positions']} position(s) have no live "
                     "mark or greeks, so their decay and P&L are missing from the "
                     "figures below — the theta total understates the real book."
                     + (" Affected: " + ", ".join(tickers) if tickers else "")),
            "payload": {"unpriced": a["unpriced"], "tickers": tickers},
            "dedupe_hours": 6,
        })

    # Summary (if positions are being actively watched).
    if a["n_positions"] > 0:
        msgs.append({
            "kind": "briefing", "priority": "normal",
            "subject": f"Portfolio: {a['n_positions']} open position(s), ${a['theta_sum']:.0f}/day theta decay",
            "body": f"Monitoring {a['n_positions']} open position(s) "
                    f"from {a.get('source') or 'unknown source'}. "
                    f"Portfolio theta decay: ${a['theta_sum']:.0f} per day. "
                    "Watch for rapid decay as expirations approach.",
            "payload": {"n_positions": a["n_positions"], "theta_sum": a["theta_sum"],
                        "source": a.get("source")},
            "dedupe_hours": 6,
        })

    return msgs


def run_scan(user_id: int | None = None, send: bool = True, book=None) -> dict:
    """One full Position Manager cycle: read the book, analyze, log, deliver mail.

    The book comes from the broker snapshot the Trader ingests, not from
    anyone's hand-kept journal — see app/portfolio.py. `book` is injectable
    so tests can drive the cycle without a store or a network.
    """
    from . import portfolio

    b = book if book is not None else portfolio.current()
    a = analyze_positions(b.get("positions") or [])
    a["source"] = b.get("source")
    a["blind"] = bool(b.get("blind"))
    a["stale"] = bool(b.get("stale"))
    a["as_of"] = b.get("as_of")
    a["age_hours"] = b.get("age_hours")
    a["unreadable"] = b.get("unreadable", 0)

    note = (f"blind — no broker snapshot to read" if a["blind"] else
            f"{a['n_positions']} position(s) from {a['source']}; "
            f"{len(a['actions'])} action(s); ${a['theta_sum']:.0f}/day theta"
            + (f"; snapshot {a['age_hours']}h old (STALE)" if a["stale"] else ""))
    tracking.log_event("position_scan", source=AGENT_ID, note=note, payload=a)

    delivered, deduped = [], 0
    if send:
        for m in compose_messages(a, user_id):
            r = tracking.send_message(AGENT_ID, TRADER_ID, **m)
            if r.get("deduped"):
                deduped += 1
            else:
                delivered.append({"id": r["id"], "subject": r["subject"],
                                  "priority": m.get("priority", "normal")})

    return {"analysis": a, "delivered": delivered, "deduped": deduped}


def _loop():
    time.sleep(60)  # let the server finish booting first
    while True:
        try:
            run_scan()
        except Exception:
            pass  # fail soft; next cycle retries
        time.sleep(CHECK_INTERVAL)


_started = False


def start_background_thread():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="position-manager").start()
