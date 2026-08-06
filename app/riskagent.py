"""The Risk Manager: agent №4, who watches portfolio-level Greeks and exposure.

Monitors aggregate delta, gamma, theta per ticker and in sum, looking for
imbalances, concentration risk, and conditions that could spike losses under
a large move. Sends alerts to the Trader over the message bus but never
calls broker tools — the Trader decides how to hedge or reduce size.

Design notes:
- analyze_portfolio() is a pure function over position totals and Greeks.
- Risk classification (low/medium/high) is deterministic and testable.
- Background loop checks every 60 min (longer than Position Manager, since
  portfolio Greeks are less volatile than individual position risk).
"""

from __future__ import annotations

import threading
import time

from . import positions, tracking, journal

AGENT_ID = "risk_manager"
TRADER_ID = "trader"
CHECK_INTERVAL = 3600  # seconds (60 min)

# Risk thresholds (per-portfolio).
GAMMA_ALERT_THRESHOLD = 1.5  # per $1 move in underlying, gamma loss >1.5% triggers alert
THETA_IMBALANCE = 100  # if |daily net theta| > this, consider rebalancing
CONCENTRATION_SINGLE_TICKER = 0.5  # if one ticker is >50% of delta, flag it
CORRELATION_RISK = 0.6  # if holdings share correlated themes, flag it


def analyze_portfolio(totals: dict, positions_list: list[dict]) -> dict:
    """Analyze portfolio-level Greeks and exposures for hidden risks."""
    issues = []

    delta = totals.get("delta_shares", 0)
    theta = totals.get("theta_per_day", 0)
    gamma = 0.0  # will compute from positions

    # Per-ticker concentration (only flag if there are multiple positions and one dominates).
    ticker_deltas = {}
    for p in positions_list:
        t = p.get("ticker")
        d = p.get("delta_shares", 0)
        if t:
            ticker_deltas[t] = ticker_deltas.get(t, 0) + d

    net_delta_risk = abs(delta)
    if net_delta_risk > 0 and len(positions_list) > 1:  # only flag if more than one position
        for ticker, tick_delta in ticker_deltas.items():
            concentration = abs(tick_delta) / net_delta_risk if net_delta_risk else 0
            if concentration > CONCENTRATION_SINGLE_TICKER:
                issues.append({
                    "type": "concentration",
                    "ticker": ticker,
                    "concentration_pct": round(concentration * 100, 1),
                    "delta_shares": round(tick_delta, 1),
                    "note": f"{ticker} is {concentration * 100:.0f}% of portfolio delta — "
                            "single-ticker risk is high. Diversify or reduce.",
                })

    # Theta imbalance (portfolio is long/short theta asymmetrically).
    if abs(theta) > THETA_IMBALANCE:
        direction = "bleeding" if theta < 0 else "collecting"
        issues.append({
            "type": "theta_imbalance",
            "theta_per_day": round(theta, 2),
            "note": f"Portfolio is {direction} ${abs(theta):.0f}/day in theta. "
                    "Large moves will be accelerated/cushioned by time decay.",
        })

    # Gross delta (total long + total short; higher = more leverage).
    long_delta = sum(p.get("delta_shares", 0) for p in positions_list if p.get("delta_shares", 0) > 0)
    short_delta = abs(sum(p.get("delta_shares", 0) for p in positions_list if p.get("delta_shares", 0) < 0))
    gross_delta = long_delta + short_delta

    if gross_delta > 2000:  # >20 deltas of leverage (option-equivalent of 20 stocks)
        issues.append({
            "type": "leverage",
            "gross_delta": round(gross_delta, 1),
            "note": f"Gross delta {gross_delta:.0f} indicates high leverage. "
                    "A 1% market move could swing portfolio significantly.",
        })

    # Gamma (aggregate vega risk from options; rough: sum of |gamma| * position delta * 100).
    # Positive gamma = benefits from realized vol; negative = suffers.
    total_gamma = 0.0
    for p in positions_list:
        if p.get("instrument") in ("call", "put"):
            # Simplified: treat gamma as a risk factor of position quantity * greeks exposure.
            # In reality this needs to be computed from the chain, but we'll flag asymmetry.
            dte = p.get("dte")
            if dte and 1 <= dte <= 14:  # highest gamma near-term and at-the-money
                total_gamma += 1 * p.get("quantity", 0)  # crude approximation

    # Directional mismatch: long gamma (ATM short-term options) vs short gamma (long-dated or OTM).
    if abs(total_gamma) > 5:
        direction = "positive (long vol)" if total_gamma > 0 else "negative (short vol)"
        issues.append({
            "type": "gamma_imbalance",
            "gamma_score": round(total_gamma, 1),
            "note": f"Aggregate gamma is {direction}. "
                    "Large realized moves will pull profit or loss faster than linear delta.",
        })

    # Unhedged directional bet (high net delta, no protective put/collar).
    if abs(delta) > 500 and abs(theta) < 10:  # high delta, little theta = naked long/short
        direction = "long" if delta > 0 else "short"
        issues.append({
            "type": "unhedged",
            "net_delta": round(delta, 1),
            "note": f"Portfolio is {direction} {abs(delta):.0f} delta with minimal theta. "
                    "No hedges in place; losses are unbounded if market reverses.",
        })

    risk_level = "low"
    if len(issues) >= 3 or any(i["type"] in ("leverage", "concentration") for i in issues):
        risk_level = "high"
    elif len(issues) >= 1:
        risk_level = "medium"

    return {
        "net_delta": round(delta, 1),
        "net_theta": round(theta, 2),
        "gross_delta": round(gross_delta, 1),
        "n_positions": len(positions_list),
        "risk_level": risk_level,
        "issues": issues,
    }


def compose_messages(a: dict) -> list[dict]:
    """Turn portfolio risk analysis into inbox-ready messages."""
    msgs = []

    # High-priority alerts for dangerous imbalances.
    for issue in a["issues"]:
        if issue["type"] in ("leverage", "concentration"):
            msgs.append({
                "kind": "alert", "priority": "high",
                "subject": f"Portfolio risk: {issue['type'].replace('_', ' ').title()}",
                "body": issue["note"],
                "payload": issue, "dedupe_hours": 12,
            })
        elif issue["type"] in ("theta_imbalance", "gamma_imbalance", "unhedged"):
            msgs.append({
                "kind": "briefing", "priority": "normal",
                "subject": f"Portfolio risk: {issue['type'].replace('_', ' ').title()}",
                "body": issue["note"],
                "payload": issue, "dedupe_hours": 12,
            })

    # Summary message.
    if a["n_positions"] > 0:
        msgs.append({
            "kind": "briefing", "priority": "normal",
            "subject": f"Portfolio Greeks: Δ{a['net_delta']:+.0f}, Θ${a['net_theta']:+.0f}/day — {a['risk_level']} risk",
            "body": (f"{a['n_positions']} open position(s). "
                     f"Net delta: {a['net_delta']:+.0f} (portfolio is {'long' if a['net_delta'] > 0 else 'short'} the market). "
                     f"Theta decay: ${a['net_theta']:+.0f}/day. Risk level: {a['risk_level']}. "
                     f"Watch for correlations and tail events."),
            "payload": {"net_delta": a["net_delta"], "net_theta": a["net_theta"],
                        "gross_delta": a["gross_delta"], "risk_level": a["risk_level"]},
            "dedupe_hours": 6,
        })

    return msgs


def run_scan(send: bool = True) -> dict:
    """One full Risk Manager cycle: gather portfolio state, analyze, log, deliver mail."""
    all_positions = []
    all_totals = None

    # Aggregate positions across all users.
    with journal._conn() as c:
        rows = c.execute("SELECT DISTINCT user_id FROM trades WHERE status = 'open' AND user_id IS NOT NULL").fetchall()
        user_ids = [r[0] for r in rows]

    for uid in user_ids:
        try:
            pos_data = positions.live_positions(uid)
            all_positions.extend(pos_data.get("positions", []))
            if all_totals is None:
                all_totals = pos_data.get("totals", {})
            else:
                # Aggregate totals.
                for k in ["delta_shares", "theta_per_day", "cost_basis", "market_value", "unrealized_pnl"]:
                    all_totals[k] = all_totals.get(k, 0) + pos_data.get("totals", {}).get(k, 0)
        except Exception:
            pass  # fail soft

    # Same fallback as the Position Manager: an empty journal does not mean an
    # empty account. Reporting LOW risk off zero rows is the worst failure this
    # agent has — a green light from a gauge that is not connected.
    #
    # Fallback rather than merge, so a position held in both sources is not
    # double-counted into twice its real delta.
    source = "journal"
    if not all_positions:
        try:
            snap = positions.broker_positions()
            if snap.get("positions"):
                all_positions = snap["positions"]
                all_totals = snap.get("totals") or all_totals
                source = snap.get("source", "tracking-store")
                if snap.get("stale"):
                    source += f" — STALE, {snap.get('age_hours')}h old"
        except Exception:
            pass

    if all_totals is None:
        all_totals = {"delta_shares": 0, "theta_per_day": 0}

    a = analyze_portfolio(all_totals, all_positions)
    a["source"] = source
    tracking.log_event(
        "risk_scan", source=AGENT_ID,
        note=(f"Portfolio Greeks: Δ{a['net_delta']:+.0f}, Θ${a['net_theta']:+.0f}/day, {a['risk_level']} risk; "
              f"{len(a['issues'])} issue(s) identified"),
        payload=a)

    delivered, deduped = [], 0
    if send:
        for m in compose_messages(a):
            r = tracking.send_message(AGENT_ID, TRADER_ID, **m)
            if r.get("deduped"):
                deduped += 1
            else:
                delivered.append({"id": r["id"], "subject": r["subject"],
                                  "priority": m.get("priority", "normal")})

    return {"analysis": a, "delivered": delivered, "deduped": deduped}


def _loop():
    time.sleep(90)  # let the server finish booting first
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
    threading.Thread(target=_loop, daemon=True, name="risk-manager").start()
