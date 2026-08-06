"""What the account actually holds, as the agents see it.

The Position Manager and Risk Manager used to read `journal.list_trades()` —
the trades a human types into the Journal tab. That made them only as good as
someone's bookkeeping, and silently wrong for anyone who doesn't keep a
journal at all: an empty table reads as "you hold nothing", which is
indistinguishable from "I cannot see your account". For a risk agent those
two are opposites, and reporting the reassuring one is the dangerous bug.

So the source of truth here is the broker: the position payloads the Trader
pulls from the Robinhood MCP server and stores with `ingest` (CLAUDE.md rule
4), which land in `agent_events` as kind='position'. From those rows this
module reconstructs the current holdings, and then gathers everything else
itself — live quotes, option marks, greeks, days to expiry, earnings dates —
so the only human input in the loop is the broker snapshot.

Why the agents can't just call Robinhood themselves: they run as daemon
threads inside the web server, and MCP tools exist only inside a Claude
session. The Trader is the bridge, exactly as the separation of duties
requires — it pulls, `ingest` records, and every agent reads from there.

Two states this module refuses to blur:
  blind  — no broker snapshot has ever been ingested. The agents know
           nothing and must say so.
  stale  — a snapshot exists but has aged past `STALE_HOURS`. Risk numbers
           computed on it may describe a portfolio you no longer hold.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import tracking

# Rows ingested within this window of the newest position event belong to the
# same snapshot. One `ingest` call stamps its rows within a second or two;
# anything older is a previous pull, and a position that appears only there
# has since been closed.
SNAPSHOT_WINDOW_SECONDS = 600

# Past this age a snapshot still renders, but every consumer is told it is
# stale so nobody presents week-old holdings as current risk.
STALE_HOURS = 12

_TICKER_KEYS = ("chain_symbol", "symbol", "ticker", "underlying_symbol",
                "instrument_symbol")
_QTY_KEYS = ("quantity", "qty", "shares", "processed_quantity")
_ENTRY_KEYS = ("average_buy_price", "average_price", "average_open_price",
               "cost_basis_per_share", "price")
_DIRECTION_KEYS = ("type", "position_type", "side", "direction")
_STRIKE_KEYS = ("strike_price", "strike")
_EXPIRY_KEYS = ("expiration_date", "expiry", "expiration")


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize(payload: dict, ticker_hint: str | None = None) -> dict | None:
    """One raw broker position payload → the shape the app's analytics use.

    Returns None when the payload carries too little to reason about (no
    ticker, or no quantity) — those are counted separately rather than
    silently treated as a flat book.
    """
    if not isinstance(payload, dict):
        return None

    ticker = _first(payload, _TICKER_KEYS) or ticker_hint
    if not ticker:
        return None
    qty = _num(_first(payload, _QTY_KEYS))
    if qty is None or qty == 0:
        return None

    opt_type = payload.get("option_type") or payload.get("contract_type")
    instrument = str(opt_type).lower() if opt_type else "stock"
    if instrument not in ("call", "put"):
        instrument = "stock"

    raw_dir = str(_first(payload, _DIRECTION_KEYS) or "").lower()
    # Robinhood marks option legs long/short; equities carry a side or a
    # signed quantity. A negative quantity is short regardless of labels.
    direction = "short" if ("short" in raw_dir or "sell" in raw_dir or qty < 0) else "long"

    expiry = _first(payload, _EXPIRY_KEYS)
    if expiry:
        expiry = str(expiry)[:10]

    return {
        "ticker": str(ticker).upper().strip(),
        "instrument": instrument,
        "direction": direction,
        "quantity": abs(qty),
        "strike": _num(_first(payload, _STRIKE_KEYS)),
        "expiry": expiry,
        "entry_price": _num(_first(payload, _ENTRY_KEYS)),
    }


def _identity(p: dict) -> tuple:
    return (p["ticker"], p["instrument"], p.get("strike"), p.get("expiry"),
            p["direction"])


def broker_snapshot(events: list[dict] | None = None,
                    window_seconds: int = SNAPSHOT_WINDOW_SECONDS,
                    now: datetime | None = None) -> dict:
    """Reconstruct current holdings from the most recent ingested pull.

    Only rows belonging to the newest snapshot count, so a position that was
    in last week's pull but not today's correctly disappears instead of
    haunting the risk numbers forever.
    """
    rows = tracking.list_events(kind="position", limit=500) if events is None else events
    if not rows:
        return {"positions": [], "as_of": None, "age_hours": None,
                "unreadable": 0, "blind": True, "stale": False}

    newest_ts = None
    for r in rows:
        t = _parse_ts(r.get("ts"))
        if t and (newest_ts is None or t > newest_ts):
            newest_ts = t
    if newest_ts is None:
        return {"positions": [], "as_of": None, "age_hours": None,
                "unreadable": len(rows), "blind": True, "stale": False}

    cutoff = newest_ts - timedelta(seconds=window_seconds)
    positions, seen, unreadable = [], set(), 0
    for r in sorted(rows, key=lambda r: r.get("ts") or "", reverse=True):
        t = _parse_ts(r.get("ts"))
        if t is None or t < cutoff:
            continue
        p = normalize(r.get("payload") or {}, ticker_hint=r.get("ticker"))
        if p is None:
            # A position event we can't reconstruct — usually logged as a
            # note without its payload. Counted, never assumed away.
            unreadable += 1
            continue
        ident = _identity(p)
        if ident in seen:
            continue
        seen.add(ident)
        p["as_of"] = r.get("ts")
        positions.append(p)

    now = now or datetime.now(timezone.utc)
    if newest_ts.tzinfo is None:
        newest_ts = newest_ts.replace(tzinfo=timezone.utc)
    age_hours = round((now - newest_ts).total_seconds() / 3600, 2)

    return {
        "positions": positions,
        "as_of": newest_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_hours": age_hours,
        "unreadable": unreadable,
        # Rows that exist but none of which could be read still leaves the
        # agents unable to see the book — that is blindness, not a flat book.
        "blind": not positions and unreadable > 0,
        "stale": age_hours > STALE_HOURS,
    }


# ---------- market context the agents gather for themselves ----------

def _live_quote(ticker: str):
    from . import data
    return data.get_quote(ticker)["price"]


def _live_option(p: dict):
    """Mark + greeks for one option leg, from the live chain."""
    from . import positions as _positions
    return _positions._mark_option(p)


def _live_earnings(ticker: str):
    from . import data
    return data.get_earnings_info(ticker).get("days_until")


def enrich(rows: list[dict], quote=None, option=None, earnings=None,
           today: date | None = None) -> list[dict]:
    """Attach live market context to broker rows.

    Every lookup is injectable so tests can exercise the maths without a
    network, and every one fails soft: a position whose chain is unavailable
    still appears with its size intact, just without greeks.
    """
    quote = quote or _live_quote
    option = option or _live_option
    earnings = earnings or _live_earnings
    today = today or date.today()

    out = []
    for r in rows:
        sign = 1 if r["direction"] == "long" else -1
        mult = 100 if r["instrument"] in ("call", "put") else 1
        p = {**r, "mark": None, "underlying_price": None, "iv": None,
             "delta_shares": None, "theta_per_day": None, "dte": None,
             "earnings_in_days": None, "unrealized_pnl": None,
             "unrealized_pnl_pct": None, "flags": []}

        try:
            p["underlying_price"] = quote(r["ticker"])
        except Exception:
            pass

        if r["instrument"] == "stock":
            p["mark"] = p["underlying_price"]
            p["delta_shares"] = round(sign * r["quantity"], 2)
            p["theta_per_day"] = 0.0
        else:
            try:
                m = option(r) or {}
            except Exception:
                m = {}
            if m.get("mark") is not None:
                p["mark"] = m["mark"]
                p["iv"] = m.get("iv")
                if m.get("delta") is not None:
                    p["delta_shares"] = round(sign * m["delta"] * r["quantity"] * 100, 1)
                if m.get("theta") is not None:
                    p["theta_per_day"] = round(sign * m["theta"] * r["quantity"] * 100, 2)
            if r.get("expiry"):
                try:
                    p["dte"] = (date.fromisoformat(r["expiry"]) - today).days
                except ValueError:
                    pass

        entry = r.get("entry_price")
        if entry is not None and p["mark"] is not None:
            basis = entry * r["quantity"] * mult
            value = p["mark"] * r["quantity"] * mult
            pnl = round(sign * (value - basis), 2)
            p["unrealized_pnl"] = pnl
            p["unrealized_pnl_pct"] = round(pnl / basis * 100, 1) if basis else None

        try:
            p["earnings_in_days"] = earnings(r["ticker"])
        except Exception:
            pass

        out.append(p)
    return out


def totals(rows: list[dict]) -> dict:
    t = {"delta_shares": 0.0, "theta_per_day": 0.0, "unrealized_pnl": 0.0}
    for p in rows:
        for k in t:
            v = p.get(k)
            if v is not None:
                t[k] += v
    return {k: round(v, 2) for k, v in t.items()}


def current(enrich_rows: bool = True, **kw) -> dict:
    """The agents' view of the book: broker snapshot first, journal fallback.

    `source` says which one answered, and `blind` says nobody could — the
    state every consumer must handle differently from an empty portfolio.
    """
    snap = broker_snapshot(**kw)
    if snap["positions"]:
        rows = enrich(snap["positions"]) if enrich_rows else snap["positions"]
        return {**snap, "source": "robinhood-mcp", "positions": rows,
                "totals": totals(rows), "blind": False}

    # Nothing from the broker. The journal is a courtesy fallback for people
    # who do keep one; for everyone else this stays empty and we report
    # blindness rather than a reassuring zero.
    journal_rows = []
    try:
        from . import journal, positions as _positions
        with journal._conn() as c:
            uids = [r[0] for r in c.execute(
                "SELECT DISTINCT user_id FROM trades WHERE status = 'open' "
                "AND user_id IS NOT NULL").fetchall()]
        for uid in uids:
            journal_rows.extend(_positions.live_positions(uid).get("positions", []))
    except Exception:
        journal_rows = []

    if journal_rows:
        return {**snap, "source": "journal", "positions": journal_rows,
                "totals": totals(journal_rows), "blind": False, "stale": False}

    return {**snap, "source": "none", "positions": [], "totals": totals([]),
            "blind": True}
