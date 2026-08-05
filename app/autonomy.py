"""Risk gate for unattended (autonomous) trading.

When the agent trades while a human is in the conversation, the human *is*
the risk check: CLAUDE.md requires an explicit yes on every order, and the
worst case gets stated out loud before it's approved. Unattended runs have
nobody to hear that warning, so the check has to live in code instead of in
a prompt — deterministic, testable, and auditable after the fact.

This module is that check. It answers one question:

    may an autonomous run place THIS specific order right now?

and returns a machine-readable verdict with reasons. Everything about it
fails closed:

- Autonomous trading is **disabled by default**; the kill switch is the
  first thing checked.
- An unrecognized strategy counts as undefined risk, not as safe.
- An unknown or unbounded max loss is a denial, not a shrug.
- Caps are per-trade *and* per-day, so one bad read can't compound while
  nobody is watching.
- A strategy may be defined-risk in theory and still be unplaceable in
  practice; ``allowed_strategies`` exists to say what the *broker* accepts,
  which is a narrower question than what is survivable.

Policy lives in ``autonomy.json`` in the repo root (gitignored — it is local
state, and a committed "enabled: true" would be a foot-gun). Missing file
means the conservative defaults below.

Deliberate limitation, stated plainly: nothing here can physically intercept
a broker call. The Robinhood MCP tools are reachable by the model directly.
This is a mandatory protocol step (CLAUDE.md requires the gate to pass before
any unattended order) backed by a deterministic implementation — stronger
than prose alone, weaker than a network-level interceptor. The daily counter
and the event log make violations visible after the fact.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import tracking

POLICY_PATH = Path(__file__).resolve().parent.parent / "autonomy.json"

# Conservative until the account owner says otherwise. Every one of these is
# a single-line change via `python -m app autonomy set`.
DEFAULTS: dict[str, Any] = {
    "enabled": False,              # kill switch — nothing autonomous runs while False
    "per_trade_max_usd": 300.0,    # max cost/max-loss for ONE unattended trade
    "max_trades_per_day": 1,       # cap on unattended orders per UTC day
    "defined_risk_only": True,     # no naked/undefined-risk positions unattended
    "require_max_loss": True,      # refuse anything whose worst case isn't a number
    "allowed_tickers": [],         # empty = no allowlist restriction
    "allowed_strategies": [],      # empty = no restriction; see LONG_ONLY below
}

# What Robinhood's agentic accounts actually accept: "long equities and
# options orders" — no spreads, no short legs. A debit spread is perfectly
# defined-risk and would still be rejected at the broker, so pass this as
# ``allowed_strategies`` to fail fast here instead of burning a daily slot on
# an order that cannot fill:
#     python -m app autonomy set --key allowed_strategies --value long_call,long_put,long_stock,stock
LONG_ONLY = ["long_call", "long_put", "long_stock", "stock"]

# Strategies whose worst case is bounded and knowable up front.
DEFINED_RISK = {
    "long_call", "long_put", "debit_spread", "credit_spread", "vertical_spread",
    "call_spread", "put_spread", "iron_condor", "butterfly", "covered_call",
    "cash_secured_put", "stock", "long_stock",
}
# Strategies whose worst case is unbounded or collateral-dependent.
UNDEFINED_RISK = {
    "naked_call", "naked_short_call", "short_call", "naked_put",
    "naked_short_put", "short_put", "short_straddle", "short_strangle",
    "short_stock", "ratio_spread",
}

AUTONOMOUS_SOURCE = "autonomous"


# ---------- policy ----------

def load_policy() -> dict:
    """Current policy: defaults overlaid with autonomy.json when present."""
    policy = dict(DEFAULTS)
    try:
        stored = json.loads(POLICY_PATH.read_text())
        if isinstance(stored, dict):
            policy.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass  # missing or corrupt file → safe defaults, never a crash
    return policy


def save_policy(updates: dict) -> dict:
    """Persist policy changes, validating types and rejecting unknown keys."""
    policy = load_policy()
    for k, v in updates.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown policy key {k!r}; valid keys: {sorted(DEFAULTS)}")
        want = type(DEFAULTS[k])
        if want is float:
            v = float(v)
        elif want is int and not isinstance(v, bool):
            v = int(v)
        elif want is bool:
            v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        elif want is list:
            items = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(","))
                     if str(x).strip()]
            # Tickers are conventionally upper; strategy names are keyed in the
            # same lower_snake form _normalize_strategy produces, so a value
            # typed as "Long Call" still matches.
            v = ([s.lower().replace("-", "_").replace(" ", "_") for s in items]
                 if k == "allowed_strategies" else [s.upper() for s in items])
        policy[k] = v
    if policy["per_trade_max_usd"] < 0 or policy["max_trades_per_day"] < 0:
        raise ValueError("caps cannot be negative")
    POLICY_PATH.write_text(json.dumps(policy, indent=2) + "\n")
    return policy


# ---------- daily counter ----------

def trades_today(now: datetime | None = None) -> int:
    """Autonomous orders already placed in the current UTC day."""
    now = now or datetime.now(timezone.utc)
    since = now.strftime("%Y-%m-%dT00:00:00Z")
    events = tracking.list_events(kind="order", since=since, limit=2000)
    return sum(1 for e in events if (e.get("source") or "") == AUTONOMOUS_SOURCE)


# ---------- the gate ----------

def _normalize_strategy(order: dict) -> str:
    """Strategy name in the canonical lower_snake form the sets are keyed by."""
    return str(order.get("strategy") or "").strip().lower().replace("-", "_").replace(" ", "_")


def _risk_kind(order: dict) -> str:
    """'defined' | 'undefined' — unknown strategies fail closed as undefined."""
    explicit = order.get("defined_risk")
    if isinstance(explicit, bool):
        return "defined" if explicit else "undefined"
    if _normalize_strategy(order) in DEFINED_RISK:
        return "defined"
    return "undefined"


def _cost_of(order: dict) -> float | None:
    """Worst-case dollars at stake: max_loss when given, else estimated cost."""
    for key in ("max_loss", "est_cost", "total_cost", "cost"):
        v = order.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f or f == float("inf"):  # NaN / infinity
            return float("inf")
        return abs(f)
    return None


def check_order(order: dict, policy: dict | None = None,
                now: datetime | None = None) -> dict:
    """Decide whether an unattended run may place ``order``.

    ``order`` is a plain dict describing the intended trade, e.g.::

        {"ticker": "NVDA", "strategy": "long_call", "quantity": 2,
         "strike": 185, "expiry": "2026-09-18", "limit_price": 4.35,
         "est_cost": 870, "max_loss": 870}

    Returns ``{"allowed": bool, "reasons": [...], "warnings": [...], ...}``.
    ``reasons`` explains every denial; an allowed order has an empty list.
    """
    policy = policy or load_policy()
    reasons: list[str] = []
    warnings: list[str] = []

    if not policy["enabled"]:
        reasons.append(
            "autonomous trading is disabled (kill switch off) — "
            "enable with `python -m app autonomy enable`")

    ticker = str(order.get("ticker") or "").upper().strip()
    if not ticker:
        reasons.append("order has no ticker")
    allowlist = policy.get("allowed_tickers") or []
    if allowlist and ticker and ticker not in allowlist:
        reasons.append(f"{ticker} is not in the autonomous allowlist {allowlist}")

    strategy_name = _normalize_strategy(order)
    strategy_allowlist = policy.get("allowed_strategies") or []
    if strategy_allowlist and strategy_name not in strategy_allowlist:
        reasons.append(
            f"strategy '{strategy_name or 'unspecified'}' is not in the autonomous "
            f"strategy allowlist {strategy_allowlist} — this list is what the broker "
            "will actually accept unattended, which is narrower than what is "
            "defined-risk")

    risk_kind = _risk_kind(order)
    if risk_kind == "undefined":
        strategy = order.get("strategy") or "unspecified"
        if policy["defined_risk_only"]:
            reasons.append(
                f"strategy '{strategy}' is undefined-risk (or unrecognized, which is "
                "treated as undefined) and defined_risk_only is on — unattended runs "
                "may only open positions whose max loss is capped and known")
        else:
            warnings.append(
                f"UNDEFINED RISK: '{strategy}' can lose more than the cost to open it "
                "(a naked short call has no loss ceiling) and no human is present to "
                "hear this warning before it executes")

    cost = _cost_of(order)
    if cost is None:
        if policy["require_max_loss"]:
            reasons.append(
                "no max_loss or est_cost given — an unattended run may not place an "
                "order whose worst case it cannot state as a number")
    elif cost == float("inf"):
        reasons.append("max loss is unbounded — never allowed unattended")
    elif cost > policy["per_trade_max_usd"]:
        reasons.append(
            f"worst case ${cost:,.2f} exceeds the per-trade autonomous cap "
            f"${policy['per_trade_max_usd']:,.2f}")

    used = trades_today(now)
    if used >= policy["max_trades_per_day"]:
        reasons.append(
            f"daily autonomous trade cap reached ({used}/{policy['max_trades_per_day']}) — "
            "no further unattended orders today")

    return {
        "allowed": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "ticker": ticker or None,
        "strategy": strategy_name or None,
        "risk_kind": risk_kind,
        "worst_case_usd": None if cost is None else (
            "unbounded" if cost == float("inf") else round(cost, 2)),
        "trades_today": used,
        "policy": policy,
    }


def record_autonomous_order(order: dict, payload: Any = None) -> dict:
    """Log an order placed by an unattended run so it counts toward the cap.

    Must be called the moment the broker tool returns. Uses the shared
    tracking store with ``source='autonomous'``, which is exactly what
    ``trades_today`` counts — so the daily cap is enforced by the same
    record that provides the audit trail.
    """
    bits = [str(order.get("side") or "").lower(), str(order.get("quantity") or ""),
            str(order.get("ticker") or "").upper()]
    if order.get("strike"):
        bits.append(f"${order['strike']} {order.get('instrument') or order.get('strategy') or ''}".strip())
    if order.get("expiry"):
        bits.append(str(order["expiry"]))
    cost = _cost_of(order)
    if cost is not None and cost != float("inf"):
        bits.append(f"(worst case ${cost:,.2f})")
    note = " ".join(b for b in bits if b)
    return tracking.log_event("order", ticker=order.get("ticker"),
                              source=AUTONOMOUS_SOURCE, note=note or None,
                              payload=payload if payload is not None else order)
