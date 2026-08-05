"""The Position Manager: agent №3, who watches open positions for exits and rolls.

Continuously scans open positions per user, monitors Greeks decay, earnings/expiry
warnings, and thesis breakdowns (reversal of signal). Findings travel to the Trader
over the shared message bus as alerts and briefings. The Position Manager logs its
surveillance but never calls broker tools — the Trader decides when to close/roll.

Design notes:
- analyze_positions() is a pure function so tests can inject fixture positions.
- run_scan() accepts optional user_id for testing; defaults to all users with
  open positions.
- Background loop checks every 30 min (longer than Analyst's 15 min, since
  position risk moves slower than headlines).
"""

from __future__ import annotations

import threading
import time
from datetime import date

from . import positions, tracking, signals

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
        "actions": actions,
        "dte_warns": dte_warns,
        "earnings_warns": earnings_warns,
    }


def compose_messages(a: dict, user_id: int | None = None) -> list[dict]:
    """Turn position analysis into inbox-ready messages for the Trader."""
    msgs = []

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

    # Summary (if positions are being actively watched).
    if a["n_positions"] > 0:
        msgs.append({
            "kind": "briefing", "priority": "normal",
            "subject": f"Portfolio: {a['n_positions']} open position(s), ${a['theta_sum']:.0f}/day theta decay",
            "body": f"Monitoring {a['n_positions']} open position(s). "
                    f"Portfolio theta decay: ${a['theta_sum']:.0f} per day. "
                    "Watch for rapid decay as expirations approach.",
            "payload": {"n_positions": a["n_positions"], "theta_sum": a["theta_sum"]},
            "dedupe_hours": 6,
        })

    return msgs


def run_scan(user_id: int | None = None, send: bool = True) -> dict:
    """One full Position Manager cycle: gather positions, analyze, log, deliver mail."""
    from . import journal

    # If user_id is None, scan all users with open positions.
    if user_id is None:
        with journal._conn() as c:
            rows = c.execute("SELECT DISTINCT user_id FROM trades WHERE status = 'open' AND user_id IS NOT NULL").fetchall()
            user_ids = [r[0] for r in rows]
    else:
        user_ids = [user_id]

    all_positions = []
    for uid in user_ids:
        try:
            pos_data = positions.live_positions(uid)
            all_positions.extend(pos_data.get("positions", []))
        except Exception:
            pass  # fail soft; next cycle retries

    a = analyze_positions(all_positions)
    tracking.log_event(
        "position_scan", source=AGENT_ID,
        note=(f"{a['n_positions']} open position(s) scanned; "
              f"{len(a['actions'])} action(s) identified; "
              f"${a['theta_sum']:.0f}/day portfolio theta decay"),
        payload=a)

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
