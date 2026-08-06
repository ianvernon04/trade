"""The Risk Manager: agent №4, who watches portfolio-level Greeks and exposure.

Monitors aggregate delta, gamma, theta per ticker and in sum, looking for
imbalances, concentration risk, and conditions that could spike losses under
a large move. Sends alerts to the Trader over the message bus but never
calls broker tools — the Trader decides how to hedge or reduce size.

Holdings come from the broker snapshot the Trader ingests (app/portfolio.py).
When no snapshot exists this agent reports blindness instead of "low risk" —
a calm risk verdict computed over a book it cannot see is the worst thing it
could say.

Design notes:
- analyze_portfolio() is a pure function over position totals and Greeks.
- Risk classification (low/medium/high) is deterministic and testable.
- Background loop checks every 60 min (longer than Position Manager, since
  portfolio Greeks are less volatile than individual position risk).
"""

from __future__ import annotations

import threading
import time

from . import tracking

AGENT_ID = "risk_manager"
TRADER_ID = "trader"
CHECK_INTERVAL = 3600  # seconds (60 min)

# Risk thresholds (per-portfolio).
GAMMA_ALERT_THRESHOLD = 1.5  # per $1 move in underlying, gamma loss >1.5% triggers alert
THETA_IMBALANCE = 100  # if |daily net theta| > this, consider rebalancing
CONCENTRATION_SINGLE_TICKER = 0.5  # if one ticker is >50% of delta, flag it
CORRELATION_RISK = 0.6  # if holdings share correlated themes, flag it


def _num(v) -> float:
    """A missing greek is 0 for arithmetic but never for judgement.

    `dict.get(k, 0)` is a trap here: enrichment sets these keys explicitly to
    None when a chain lookup fails, so the default never fires and the sum
    raises. Positions that land here are counted as `unpriced` and reported,
    because a portfolio total that quietly omits a leg understates risk.
    """
    return v if isinstance(v, (int, float)) else 0.0


def analyze_portfolio(totals: dict, positions_list: list[dict]) -> dict:
    """Analyze portfolio-level Greeks and exposures for hidden risks."""
    issues = []

    delta = _num(totals.get("delta_shares"))
    theta = _num(totals.get("theta_per_day"))

    # Positions whose greeks could not be fetched: their exposure is real but
    # absent from every number below.
    unpriced = [p for p in positions_list if p.get("delta_shares") is None]

    # Per-ticker concentration (only flag if there are multiple positions and one dominates).
    ticker_deltas = {}
    for p in positions_list:
        t = p.get("ticker")
        d = _num(p.get("delta_shares"))
        if t:
            ticker_deltas[t] = ticker_deltas.get(t, 0) + d

    # Concentration is a statement about how the book splits, so it needs at
    # least two positions we could actually price. Claiming "AAPL is 100% of
    # delta" when AAPL is the only leg with greeks is an artefact, not a risk.
    priced_count = len(positions_list) - len(unpriced)
    net_delta_risk = abs(delta)
    if net_delta_risk > 0 and priced_count > 1:
        for ticker, tick_delta in ticker_deltas.items():
            concentration = abs(tick_delta) / net_delta_risk if net_delta_risk else 0
            if concentration > CONCENTRATION_SINGLE_TICKER:
                issues.append({
                    "type": "concentration",
                    "ticker": ticker,
                    "concentration_pct": round(concentration * 100, 1),
                    "delta_shares": round(tick_delta, 1),
                    "over_partial_book": bool(unpriced),
                    "note": f"{ticker} is {concentration * 100:.0f}% of portfolio delta — "
                            "single-ticker risk is high. Diversify or reduce."
                            + (f" (Measured over the {len(positions_list) - len(unpriced)} "
                               f"of {len(positions_list)} position(s) that could be priced, "
                               "so the true split may differ.)" if unpriced else ""),
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
    deltas = [_num(p.get("delta_shares")) for p in positions_list]
    gross_delta = sum(abs(d) for d in deltas)

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
                total_gamma += _num(p.get("quantity"))  # crude approximation

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

    # Exposure the numbers above do not contain. Reported before the verdict
    # so nobody reads a "low risk" computed over a partial book.
    if unpriced:
        issues.append({
            "type": "unpriced",
            "n_unpriced": len(unpriced),
            "tickers": sorted({p.get("ticker") for p in unpriced if p.get("ticker")}),
            "note": (f"{len(unpriced)} of {len(positions_list)} position(s) could not be "
                     "priced (no live chain/quote), so their delta and theta are missing "
                     "from every figure here. The real exposure is larger than shown."),
        })

    # Severity comes from what we found in the book we could read; the
    # unpriced caveat is handled separately so it can't be mistaken for a
    # finding that makes the portfolio look merely "medium".
    findings = [i for i in issues if i["type"] != "unpriced"]
    if len(findings) >= 3 or any(i["type"] in ("leverage", "concentration") for i in findings):
        risk_level = "high"
    elif findings:
        risk_level = "medium"
    else:
        risk_level = "low"
    # A book we could only partly price has no honest low/medium verdict —
    # unless something genuinely dangerous already surfaced in the priced
    # part, in which case that danger is the more useful headline.
    if unpriced and risk_level != "high":
        risk_level = "unknown"

    return {
        "unpriced": len(unpriced),
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

    # "Low risk" computed over a book we cannot see is the most dangerous
    # sentence this agent could send. Say the truth instead.
    if a.get("blind"):
        return [{
            "kind": "alert", "priority": "high",
            "subject": "Risk Manager is blind — no broker snapshot",
            "body": ("No readable Robinhood position data has been ingested, so "
                     "I cannot compute portfolio delta, theta, concentration or "
                     "leverage. This is not a flat book — it is an unknown one. "
                     "Pull positions from the Robinhood MCP server and record "
                     "them with `python3 -m app ingest --payload -`, then re-run "
                     "`python3 -m app risk-scan`."),
            "payload": {"blind": True}, "dedupe_hours": 6,
        }]

    if a.get("stale"):
        msgs.append({
            "kind": "alert", "priority": "high",
            "subject": "Risk read is running on a stale snapshot",
            "body": (f"Portfolio Greeks below were computed from position data "
                     f"{a.get('age_hours')}h old (as of {a.get('as_of')}). "
                     "Re-ingest before sizing anything off these numbers."),
            "payload": {"age_hours": a.get("age_hours"), "as_of": a.get("as_of")},
            "dedupe_hours": 6,
        })

    # High-priority alerts for dangerous imbalances.
    for issue in a["issues"]:
        if issue["type"] in ("leverage", "concentration"):
            msgs.append({
                "kind": "alert", "priority": "high",
                "subject": f"Portfolio risk: {issue['type'].replace('_', ' ').title()}",
                "body": issue["note"],
                "payload": issue, "dedupe_hours": 12,
            })
        elif issue["type"] == "unpriced":
            msgs.append({
                "kind": "alert", "priority": "high",
                "subject": "Portfolio risk: positions could not be priced",
                "body": issue["note"] + (
                    " Affected: " + ", ".join(issue["tickers"]) if issue["tickers"] else ""),
                "payload": issue, "dedupe_hours": 6,
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
            "body": (f"{a['n_positions']} open position(s) from "
                     f"{a.get('source') or 'unknown source'}. "
                     f"Net delta: {a['net_delta']:+.0f} (portfolio is {'long' if a['net_delta'] > 0 else 'short'} the market). "
                     f"Theta decay: ${a['net_theta']:+.0f}/day. Risk level: {a['risk_level']}. "
                     f"Watch for correlations and tail events."),
            "payload": {"net_delta": a["net_delta"], "net_theta": a["net_theta"],
                        "gross_delta": a["gross_delta"], "risk_level": a["risk_level"]},
            "dedupe_hours": 6,
        })

    return msgs


def run_scan(send: bool = True, book=None) -> dict:
    """One full Risk Manager cycle: read the book, analyze, log, deliver mail.

    Holdings come from the broker snapshot the Trader ingests rather than a
    hand-kept journal (app/portfolio.py). `book` is injectable for tests.
    """
    from . import portfolio

    b = book if book is not None else portfolio.current()
    a = analyze_portfolio(b.get("totals") or {}, b.get("positions") or [])
    a["source"] = b.get("source")
    a["blind"] = bool(b.get("blind"))
    a["stale"] = bool(b.get("stale"))
    a["as_of"] = b.get("as_of")
    a["age_hours"] = b.get("age_hours")

    note = ("blind — no broker snapshot to read" if a["blind"] else
            f"Δ{a['net_delta']:+.0f}, Θ${a['net_theta']:+.0f}/day from {a['source']}; "
            f"{a['risk_level']} risk; {len(a['issues'])} issue(s)"
            + (f"; snapshot {a['age_hours']}h old (STALE)" if a["stale"] else ""))
    tracking.log_event("risk_scan", source=AGENT_ID, note=note, payload=a)

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
