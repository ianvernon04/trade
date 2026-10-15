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
import math
from datetime import date, datetime, timedelta, timezone
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

# Strategies whose worst case *is* the debit paid: limit × quantity (× 100
# for a contract). For these the gate computes the number itself and ignores
# whatever the caller claimed — an audit found ``"max_loss": 0`` on a $20,000
# order sailed through, because the old gate took the caller's word for it.
LONG_STRATEGIES = set(LONG_ONLY)
STOCK_STRATEGIES = {"stock", "long_stock", "short_stock"}

AUTONOMOUS_SOURCE = "autonomous"

# Reservation lifecycle, stored in the event payload's ``status`` field:
#   reserved   — slot taken, broker not yet called (or the run died in between)
#   placed     — broker result attached by record_autonomous_order
#   released   — slot given back by release_slot; the broker was never called
#   reconciled_unrecorded — a fill the broker reports that nothing here logged
# Every status except "released" counts toward the daily cap, and so does a
# legacy row with no status at all. A reservation nobody resolved is the
# crash case, and the crash case is exactly when the cap must hold.
RESERVED, PLACED, RELEASED, RECONCILED = "reserved", "placed", "released", "reconciled_unrecorded"

# Broker order states after which nothing more can happen to the order.
TERMINAL_STATES = {"filled", "cancelled", "canceled", "rejected", "failed",
                   "expired", "voided", "replaced"}
RECONCILE_WINDOW_DAYS = 3


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

def _counts_toward_cap(event: dict) -> bool:
    """Does this ``source='autonomous'`` order row consume a daily slot?

    Everything does except a reservation explicitly released before the
    broker was called. A reservation still sitting in ``reserved`` counts:
    the run may have died *after* the broker accepted the order, and the cap
    has to assume it did.
    """
    if (event.get("source") or "") != AUTONOMOUS_SOURCE:
        return False
    payload = event.get("payload")
    status = payload.get("status") if isinstance(payload, dict) else None
    return status != RELEASED


def _day_start(now: datetime | None) -> tuple[datetime, str]:
    now = now or datetime.now(timezone.utc)
    return now, now.strftime("%Y-%m-%dT00:00:00Z")


def trades_today(now: datetime | None = None) -> int:
    """Autonomous order slots used in the current UTC day.

    Counts ``kind='order', source='autonomous'`` rows — reserved, placed, or
    reconciled — and skips released reservations. See ``_counts_toward_cap``.
    """
    _, since = _day_start(now)
    events = tracking.list_events(kind="order", since=since, limit=2000)
    return sum(1 for e in events if _counts_toward_cap(e))


# ---------- the gate ----------

def _normalize_strategy(order: dict) -> str:
    """Strategy name in the canonical lower_snake form the sets are keyed by."""
    return str(order.get("strategy") or "").strip().lower().replace("-", "_").replace(" ", "_")


def _is_option(order: dict, strategy_name: str) -> bool:
    """Stock strategies are shares; known option strategies are contracts; an
    unknown strategy is an option if it carries option fields."""
    if strategy_name in STOCK_STRATEGIES:
        return False
    if strategy_name in DEFINED_RISK or strategy_name in UNDEFINED_RISK:
        return True
    return any(order.get(k) not in (None, "") for k in ("strike", "expiry", "option_type"))


def _positive_number(value: Any) -> float | None:
    """``value`` as a finite float > 0, else None. Booleans are not numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _parse_expiry(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


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

    The worst case is **computed here, not read from the order**. For a long
    strategy it is ``limit_price × quantity`` (× 100 per contract), and the
    caller's ``max_loss`` is checked against that number rather than trusted:
    missing, non-positive, or understated all deny. Only limit, day orders
    pass — a market order has no computable worst case, and a GTC order
    outlives the run that placed it. Options need a future expiry (no 0DTE).
    The computed figures come back in the result so the model can see what
    the gate actually measured.
    """
    policy = policy or load_policy()
    now = now or datetime.now(timezone.utc)
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

    # --- order shape: only a limit, day order with a positive size passes ---
    is_long = strategy_name in LONG_STRATEGIES
    is_option = _is_option(order, strategy_name)
    side = str(order.get("side") or "").strip().lower()
    if is_long and side != "buy":
        reasons.append(
            f"side must be 'buy' for {strategy_name} (got {side or 'nothing'}) — "
            "an unattended run opens long positions only; it never sells")

    order_type = str(order.get("order_type") or "limit").strip().lower()
    if order_type != "limit":
        reasons.append(
            f"order_type '{order_type}' is not allowed unattended — only a limit "
            "order has a worst case that can be computed before it is placed")
    tif = str(order.get("time_in_force") or "day").strip().lower()
    if tif not in ("day", "gfd"):
        reasons.append(
            f"time_in_force '{tif}' is not allowed unattended — a resting order "
            "would outlive the run that placed it; use day/gfd")

    quantity = _positive_number(order.get("quantity"))
    if quantity is None:
        reasons.append("quantity must be a number greater than zero")
    elif is_option and not float(quantity).is_integer():
        reasons.append(f"quantity {quantity} must be a whole number of contracts")
    limit_price = _positive_number(order.get("limit_price"))
    if limit_price is None:
        reasons.append("limit_price must be a number greater than zero")

    expiry = _parse_expiry(order.get("expiry")) if order.get("expiry") not in (None, "") else None
    if is_option:
        if order.get("expiry") in (None, ""):
            reasons.append("options orders must state an expiry (YYYY-MM-DD)")
        elif expiry is None:
            reasons.append(f"expiry {order.get('expiry')!r} is not a YYYY-MM-DD date")
        elif expiry <= now.date():
            reasons.append(
                f"expiry {expiry.isoformat()} is today or in the past — no 0DTE or "
                "expired contracts unattended")

    # --- worst case: computed from the order, never taken on faith ---
    multiplier = 100 if is_option else 1
    computed_cost = (round(limit_price * quantity * multiplier, 2)
                     if limit_price is not None and quantity is not None else None)

    raw_claim = order.get("max_loss")
    claimed: float | None
    try:
        claimed = None if raw_claim is None or isinstance(raw_claim, bool) else float(raw_claim)
    except (TypeError, ValueError):
        claimed = None
        reasons.append(f"max_loss {raw_claim!r} is not a number")
    if claimed is not None and not math.isfinite(claimed):
        claimed = float("inf")

    if claimed is None:
        if policy["require_max_loss"]:
            reasons.append(
                "no max_loss given — an unattended run may not place an order whose "
                "worst case it cannot state as a number")
    elif claimed == float("inf"):
        reasons.append("max loss is unbounded — never allowed unattended")
    elif claimed <= 0:
        reasons.append(f"max_loss {claimed} is not positive — every order can lose money")
    elif is_long and computed_cost is not None and claimed < computed_cost - 0.01:
        reasons.append(
            f"max_loss ${claimed:,.2f} understates the order: "
            f"{limit_price} × {quantity:g} × {multiplier} = ${computed_cost:,.2f} at risk")

    # A long order's worst case is its cost, whatever the caller said. For any
    # other strategy the claimed max_loss is still the measure, as before.
    if is_long:
        worst = computed_cost if computed_cost is not None else (
            claimed if claimed == float("inf") else None)
    else:
        worst = claimed
    if worst is not None and worst != float("inf") and worst > policy["per_trade_max_usd"]:
        reasons.append(
            f"worst case ${worst:,.2f} exceeds the per-trade autonomous cap "
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
        "side": side or None,
        "order_type": order_type,
        "time_in_force": tif,
        "is_option": is_option,
        "quantity": quantity,
        "limit_price": limit_price,
        "multiplier": multiplier,
        "expiry": expiry.isoformat() if expiry else None,
        "computed_cost_usd": computed_cost,
        "claimed_max_loss_usd": None if claimed is None else (
            "unbounded" if claimed == float("inf") else round(claimed, 2)),
        "worst_case_usd": None if worst is None else (
            "unbounded" if worst == float("inf") else round(worst, 2)),
        "trades_today": used,
        "policy": policy,
    }


# ---------- slot reservation ----------
#
# The daily cap used to count orders *after* the broker tool returned. A run
# that died between placement and recording (it happened: 2026-08-28) left a
# live order nothing counted, and the next run was free to add another. So the
# slot is now taken *before* the broker call, in the same transaction as the
# count, and the broker result is attached to that same row afterwards. The
# worst outcome of a crash is now an over-count (a slot held for an order
# that was never sent), which costs one quiet day — never an under-count.

def _order_note(order: dict, prefix: str | None = None) -> str | None:
    bits = [prefix or "", str(order.get("side") or "").lower(),
            str(order.get("quantity") or ""), str(order.get("ticker") or "").upper()]
    if order.get("strike"):
        bits.append(f"${order['strike']} {order.get('instrument') or order.get('strategy') or ''}".strip())
    if order.get("expiry"):
        bits.append(str(order["expiry"]))
    cost = _cost_of(order)
    if cost is not None and cost != float("inf"):
        bits.append(f"(worst case ${cost:,.2f})")
    return " ".join(b for b in bits if b) or None


def _load_row(c, event_id: int) -> dict | None:
    row = c.execute("SELECT * FROM agent_events WHERE id = ?", (int(event_id),)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = tracking._load(d["payload"])
    return d


def reserve_slot(order: dict, policy: dict | None = None,
                 now: datetime | None = None) -> dict:
    """Take one daily slot for ``order`` — **before** the broker is called.

    Runs ``check_order`` first (a denied order never reserves), then, in one
    exclusive transaction, re-counts today's slots and inserts a
    ``kind='order', source='autonomous'`` row with payload
    ``{"status": "reserved", ...order}``. That row counts toward the cap
    immediately and keeps counting until ``release_slot`` says the broker
    was never called. A run that crashes after reserving leaves the slot
    taken, which is the intended failure mode.

    Returns ``{"reserved": bool, "reservation_id": int | None, "reasons":
    [...], "trades_today": int, "check": <check_order verdict>}``.
    """
    policy = policy or load_policy()
    now, since = _day_start(now)
    verdict = check_order(order, policy=policy, now=now)
    if not verdict["allowed"]:
        return {"reserved": False, "reservation_id": None, "reasons": verdict["reasons"],
                "trades_today": verdict["trades_today"], "check": verdict}

    payload = {"status": RESERVED, "reserved_at": tracking._now(), **order}
    with tracking.write_transaction() as c:
        rows = c.execute(
            "SELECT * FROM agent_events WHERE kind = 'order' AND source = ? AND ts >= ?",
            (AUTONOMOUS_SOURCE, since)).fetchall()
        used = sum(1 for r in rows if _counts_toward_cap(
            {**dict(r), "payload": tracking._load(r["payload"])}))
        if used >= policy["max_trades_per_day"]:
            reason = (f"daily autonomous trade cap reached ({used}/"
                      f"{policy['max_trades_per_day']}) — no further unattended orders today")
            return {"reserved": False, "reservation_id": None, "reasons": [reason],
                    "trades_today": used, "check": verdict}
        ticker = str(order.get("ticker") or "").upper().strip() or None
        cur = c.execute(
            "INSERT INTO agent_events (ts, kind, ticker, source, note, payload) "
            "VALUES (?, 'order', ?, ?, ?, ?)",
            (now.strftime("%Y-%m-%dT%H:%M:%SZ"), ticker, AUTONOMOUS_SOURCE,
             _order_note(order, "RESERVED"), json.dumps(payload)))
        rid = cur.lastrowid
    return {"reserved": True, "reservation_id": rid, "reasons": [],
            "trades_today": used + 1, "check": verdict}


def release_slot(reservation_id: int, reason: str) -> dict:
    """Give a reserved slot back because the broker was **never called**.

    Only a ``reserved`` row can be released; a ``placed`` one holds a real
    order and stays counted. Releasing is idempotent. Never call this after
    the order tool has been invoked — if the result is unknown, leave the
    reservation standing and let ``reconcile`` sort it out against the
    broker's order list.
    """
    with tracking.write_transaction() as c:
        row = _load_row(c, reservation_id)
        if row is None or row["kind"] != "order" or row["source"] != AUTONOMOUS_SOURCE:
            raise ValueError(f"reservation #{reservation_id} not found")
        payload = row["payload"] if isinstance(row["payload"], dict) else {"raw": row["payload"]}
        status = payload.get("status")
        if status == RELEASED:
            return {"released": True, "reservation_id": row["id"], "already": True}
        if status != RESERVED:
            return {"released": False, "reservation_id": row["id"],
                    "reason": f"reservation #{row['id']} is '{status}', not reserved — "
                              "a placed order cannot be released"}
        payload = {**payload, "status": RELEASED, "released_at": tracking._now(),
                   "release_reason": reason}
        c.execute("UPDATE agent_events SET payload = ?, note = ? WHERE id = ?",
                  (json.dumps(payload), _order_note(payload, f"RELEASED ({reason})"), row["id"]))
    return {"released": True, "reservation_id": row["id"], "already": False}


def record_autonomous_order(order: dict | None, payload: Any = None,
                            reservation_id: int | None = None) -> dict:
    """Attach the broker's result to the reservation the moment the tool returns.

    With ``reservation_id`` the reserved row is updated in place to
    ``status='placed'`` with the broker result under ``broker_result`` — one
    row, counted once. ``order`` may be None, in which case the reserved
    order is used. Without a reservation this still writes a counted row
    (the pre-reservation behaviour, kept so nothing that already works
    breaks) but flags it in the note: an order placed without a reservation
    was placed without the crash-safe count.
    """
    if reservation_id is not None:
        with tracking.write_transaction() as c:
            row = _load_row(c, reservation_id)
            if row is None or row["kind"] != "order" or row["source"] != AUTONOMOUS_SOURCE:
                row = None
            if row is not None:
                prev = row["payload"] if isinstance(row["payload"], dict) else {}
                base = {k: v for k, v in prev.items()
                        if k not in ("status", "broker_result", "placed_at")}
                merged = {**base, **(order or {}), "status": PLACED,
                          "placed_at": tracking._now(), "broker_result": payload}
                prefix = "PLACED"
                if prev.get("status") != RESERVED:
                    prefix = f"PLACED (warning: reservation was '{prev.get('status')}')"
                note = _order_note(merged, prefix)
                c.execute("UPDATE agent_events SET payload = ?, note = ?, ticker = ? WHERE id = ?",
                          (json.dumps(merged), note,
                           str(merged.get("ticker") or "").upper().strip() or row["ticker"],
                           row["id"]))
                return {**row, "payload": merged, "note": note}
        warn = f"warning: reservation #{reservation_id} not found; recorded unreserved"
    else:
        warn = "warning: recorded without a slot reservation (not crash-safe)"
    order = order or {}
    note = _order_note(order)
    return tracking.log_event("order", ticker=order.get("ticker"),
                              source=AUTONOMOUS_SOURCE,
                              note=f"{note} — {warn}" if note else warn,
                              payload=payload if payload is not None else order)


# ---------- reconciliation against the broker ----------
#
# The gate sees only what this store was told. The broker's order list is the
# ground truth, so before placing anything an unattended run hands that list
# here and lets code — not the model reading JSON by eye — decide whether an
# order is still working or a fill slipped past the record.

_ORDER_LIST_KEYS = ("equity_orders", "option_orders", "stock_orders",
                    "options_orders", "orders", "results", "items", "data")
_ORDER_TS_KEYS = ("created_at", "updated_at", "last_transaction_at",
                  "submitted_at", "timestamp", "ts")


def _collect_orders(obj: Any, out: list, depth: int = 0) -> None:
    """Flatten every order-looking dict out of whatever shape the tools gave."""
    if depth > 4:
        return
    if isinstance(obj, list):
        for item in obj:
            _collect_orders(item, out, depth + 1)
    elif isinstance(obj, dict):
        lists = [k for k in _ORDER_LIST_KEYS if isinstance(obj.get(k), (list, dict))]
        if lists and not (obj.get("id") or obj.get("order_id")):
            for k in lists:
                _collect_orders(obj[k], out, depth + 1)
        elif obj.get("id") or obj.get("order_id") or obj.get("state") or obj.get("status"):
            out.append(obj)


def parse_broker_orders(payload: Any) -> list[dict]:
    """Accept a list, ``{"equity_orders": [...], "option_orders": [...]}``,
    ``{"orders": [...]}``, nested ``{"data": {...}}`` envelopes, or a JSON
    string of any of those."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    out: list[dict] = []
    _collect_orders(payload, out)
    return out


def _order_id(o: dict) -> str | None:
    v = o.get("id") or o.get("order_id")
    return str(v) if v not in (None, "") else None


def _order_state(o: dict) -> str:
    return str(o.get("state") or o.get("status") or "").strip().lower()


def _order_ts(o: dict) -> datetime | None:
    for k in _ORDER_TS_KEYS:
        v = o.get(k)
        if not v:
            continue
        text = str(v).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _ids_in(obj: Any, out: set, depth: int = 0) -> None:
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k in ("id", "order_id"):
            if obj.get(k) not in (None, ""):
                out.add(str(obj[k]))
        for v in obj.values():
            _ids_in(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _ids_in(v, out, depth + 1)


def _recorded_order_ids(since: str) -> set[str]:
    """Every broker order id this store has seen in an order/fill event, from
    any source — a human-approved order logged per CLAUDE.md rule 4 is
    recorded, and must not be mistaken for an unrecorded autonomous fill."""
    ids: set[str] = set()
    for kind in ("order", "fill"):
        for e in tracking.list_events(kind=kind, since=since, limit=2000):
            _ids_in(e.get("payload"), ids)
    return ids


def _brief(o: dict) -> dict:
    ticker = o.get("chain_symbol") or o.get("symbol") or o.get("ticker")
    ts = _order_ts(o)
    return {"id": _order_id(o), "state": _order_state(o),
            "ticker": str(ticker).upper() if ticker else None,
            "side": o.get("side"), "quantity": o.get("quantity"),
            "created_at": ts.isoformat() if ts else None}


def reconcile_orders(payload: Any, now: datetime | None = None,
                     days: int = RECONCILE_WINDOW_DAYS) -> dict:
    """Compare the broker's recent orders with what this store recorded.

    - Any order in a **non-terminal** state (queued, confirmed, partially
      filled, open, pending — or any state not known to be terminal) blocks,
      whatever its age: it can still fill, and this run cannot cancel it.
    - A **filled** order from the last ``days`` days whose id appears in no
      recorded order/fill event is logged here as a ``source='autonomous'``
      order with ``status='reconciled_unrecorded'`` — so it counts toward
      today's cap from now on — and blocks this run.
    - A note event records what was parsed, for the audit trail.

    ``ok`` is True only when nothing is open and nothing is unrecorded.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    orders = parse_broker_orders(payload)
    recorded = _recorded_order_ids((cutoff - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    open_orders, unrecorded, matched, ignored = [], [], [], 0
    for o in orders:
        state, oid, ts = _order_state(o), _order_id(o), _order_ts(o)
        if state not in TERMINAL_STATES:
            open_orders.append({**_brief(o), "why": "unknown state" if not state else "working"})
            continue
        in_window = ts is None or ts >= cutoff  # no timestamp → assume recent
        if state != "filled" or not in_window:
            ignored += 1
            continue
        if oid and oid in recorded:
            matched.append(_brief(o))
            continue
        ev = tracking.log_event(
            "order", ticker=_brief(o)["ticker"], source=AUTONOMOUS_SOURCE,
            note=f"RECONCILED: broker fill {oid or '(no id)'} had no local record",
            payload={"status": RECONCILED, "reconciled_at": tracking._now(), "broker_order": o})
        unrecorded.append({**_brief(o), "event_id": ev["id"]})

    reasons = []
    if open_orders:
        reasons.append(f"open orders exist ({len(open_orders)}) — this run cannot cancel; stand down")
    if unrecorded:
        reasons.append(f"unrecorded fill found ({len(unrecorded)}), standing down")
    result = {"ok": not reasons, "reasons": reasons, "parsed": len(orders),
              "open_orders": open_orders, "unrecorded_fills": unrecorded,
              "matched": matched, "ignored": ignored, "window_days": days}
    tracking.log_event("note", source=AUTONOMOUS_SOURCE,
                       note=f"reconcile: {len(orders)} broker order(s), {len(open_orders)} open, "
                            f"{len(unrecorded)} unrecorded fill(s), {len(matched)} matched"
                            + ("" if not reasons else " — STAND DOWN"),
                       payload=result)
    return result
