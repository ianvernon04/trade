"""Agent activity tracker: every action, decision, and outcome, recorded.

The journal (journal.py) stores trades a human logs by hand in the UI. This
module is its counterpart for the agent loop: it records what the agent does
— analyses run, recommendations made, orders relayed through the Robinhood
MCP server, data pulled — and then grades every directional call against what
the market actually did afterwards. Over time that builds two datasets:

- ``agent_events``:    an append-only log of actions (orders, fills, data
                       pulls, notes), each with its raw payload preserved.
- ``agent_decisions``: every prediction/recommendation with the full signal
                       snapshot at decision time, later stamped with the
                       realized forward return and a hit/miss grade.

Design notes:
- Lives in the same SQLite file as the journal (one file to back up), but in
  its own tables and with its own init().
- No pandas/yfinance imports: logging must work offline and in tests, so
  price history for grading is *injected* by the caller as a plain
  ``bars_for(ticker) -> [(date, close), ...]`` callable.
- Timestamps are UTC ISO-8601 strings; decision dates are matched to daily
  bars by "first bar on or after the decision's UTC date", which is accurate
  to within a session for US equities.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "journal.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                -- analysis | order | fill | position | data | note | ...
    ticker TEXT,
    source TEXT NOT NULL DEFAULT 'agent',   -- agent | robinhood-mcp | api | user
    note TEXT,
    payload TEXT,                      -- raw JSON exactly as collected
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,              -- buy | sell | call | put | hold | no_trade
    horizon_days INTEGER NOT NULL DEFAULT 10,   -- grading horizon in trading bars
    price REAL,                        -- underlying price seen at decision time
    score REAL,                        -- composite signal score at decision time
    label TEXT,                        -- signal label at decision time
    rationale TEXT,
    signal TEXT,                       -- full signal snapshot JSON (votes etc.)
    source TEXT NOT NULL DEFAULT 'agent',
    order_id TEXT,                     -- broker/MCP order id when one exists
    evaluated_at TEXT,                 -- set once graded
    eval_price REAL,
    fwd_return_pct REAL,
    hit INTEGER,                       -- 1/0 for directional calls, NULL for flat calls
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_events_ts ON agent_events (ts);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_ticker ON agent_decisions (ticker, ts);
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    from_agent TEXT NOT NULL,          -- analyst | trader | user | ...
    to_agent TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'briefing',   -- briefing | alert | note
    priority TEXT NOT NULL DEFAULT 'normal', -- normal | high
    ticker TEXT,
    subject TEXT NOT NULL,
    body TEXT,
    payload TEXT,
    read_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages (to_agent, read_at);
"""

ACTIONS = ("buy", "sell", "call", "put", "hold", "no_trade")
# Sign of the price move each action predicts; flat calls have no sign.
DIRECTION = {"buy": 1, "call": 1, "sell": -1, "put": -1}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(obj: Any) -> str | None:
    if obj is None:
        return None
    return obj if isinstance(obj, str) else json.dumps(obj)


def _load(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


# ---------- events ----------

def log_event(kind: str, ticker: str | None = None, source: str = "agent",
              note: str | None = None, payload: Any = None,
              ts: str | None = None) -> dict:
    row = {
        "ts": ts or _now(),
        "kind": (kind or "note").strip().lower(),
        "ticker": ticker.upper().strip() if ticker else None,
        "source": source or "agent",
        "note": note,
        "payload": _dump(payload),
    }
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO agent_events (ts, kind, ticker, source, note, payload) "
            "VALUES (:ts, :kind, :ticker, :source, :note, :payload)", row)
        row["id"] = cur.lastrowid
    row["payload"] = _load(row["payload"])
    return row


def list_events(ticker: str | None = None, kind: str | None = None,
                since: str | None = None, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM agent_events WHERE 1=1", []
    if ticker:
        q += " AND ticker = ?"
        args.append(ticker.upper().strip())
    if kind:
        q += " AND kind = ?"
        args.append(kind.strip().lower())
    if since:
        q += " AND ts >= ?"
        args.append(since)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 2000)))
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["payload"] = _load(r["payload"])
    return rows


# ---------- decisions ----------

def log_decision(ticker: str, action: str, horizon_days: int = 10,
                 price: float | None = None, score: float | None = None,
                 label: str | None = None, rationale: str | None = None,
                 signal: Any = None, source: str = "agent",
                 order_id: str | None = None, ts: str | None = None) -> dict:
    action = (action or "").strip().lower().replace(" ", "_").replace("-", "_")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
    if not ticker or not ticker.strip():
        raise ValueError("ticker is required")
    row = {
        "ts": ts or _now(),
        "ticker": ticker.upper().strip(),
        "action": action,
        "horizon_days": max(1, int(horizon_days)),
        "price": price,
        "score": score,
        "label": label,
        "rationale": rationale,
        "signal": _dump(signal),
        "source": source or "agent",
        "order_id": order_id,
    }
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO agent_decisions (ts, ticker, action, horizon_days, price, score, "
            "label, rationale, signal, source, order_id) VALUES (:ts, :ticker, :action, "
            ":horizon_days, :price, :score, :label, :rationale, :signal, :source, :order_id)",
            row)
        row["id"] = cur.lastrowid
    row["signal"] = _load(row["signal"])
    return row


def list_decisions(ticker: str | None = None, action: str | None = None,
                   pending_only: bool = False, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM agent_decisions WHERE 1=1", []
    if ticker:
        q += " AND ticker = ?"
        args.append(ticker.upper().strip())
    if action:
        q += " AND action = ?"
        args.append(action.strip().lower())
    if pending_only:
        q += " AND evaluated_at IS NULL"
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 2000)))
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["signal"] = _load(r["signal"])
    return rows


# ---------- grading ----------

def evaluate_decisions(bars_for: Callable[[str], list[tuple[str, float]]]) -> dict:
    """Grade every ungraded decision whose horizon has fully elapsed.

    ``bars_for(ticker)`` must return daily bars as ``[(YYYY-MM-DD, close), ...]``
    in ascending date order (the CLI and API inject a Yahoo-backed one; tests
    inject fixtures). A decision matures once ``horizon_days`` bars exist
    after its entry bar; the forward return is measured from the recorded
    decision price (or the entry bar's close when none was recorded) to the
    close ``horizon_days`` bars later. Directional actions are graded
    hit/miss; hold/no_trade get the return stamped but no grade.
    """
    pending = list_decisions(pending_only=True, limit=2000)
    bars_cache: dict[str, list[tuple[str, float]]] = {}
    graded: list[dict] = []
    errors: dict[str, str] = {}
    immature = 0

    for d in pending:
        t = d["ticker"]
        if t in errors:
            continue
        if t not in bars_cache:
            try:
                bars_cache[t] = list(bars_for(t) or [])
            except Exception as e:  # rate limit / unknown symbol — keep going
                errors[t] = str(e)
                continue
        bars = bars_cache[t]
        day = (d["ts"] or "")[:10]
        entry_idx = next((i for i, (bd, _) in enumerate(bars) if bd >= day), None)
        if entry_idx is None or entry_idx + d["horizon_days"] >= len(bars):
            immature += 1
            continue
        entry_price = d["price"] or bars[entry_idx][1]
        exit_price = bars[entry_idx + d["horizon_days"]][1]
        if not entry_price:
            immature += 1
            continue
        fwd = (exit_price / entry_price - 1) * 100
        sign = DIRECTION.get(d["action"])
        hit = None if sign is None else int((fwd > 0) if sign > 0 else (fwd < 0))
        stamp = _now()
        with _lock, _conn() as c:
            c.execute(
                "UPDATE agent_decisions SET evaluated_at = ?, eval_price = ?, "
                "fwd_return_pct = ?, hit = ? WHERE id = ?",
                (stamp, round(exit_price, 4), round(fwd, 2), hit, d["id"]))
        graded.append({**d, "evaluated_at": stamp, "eval_price": round(exit_price, 4),
                       "fwd_return_pct": round(fwd, 2), "hit": hit})

    return {"graded": graded, "evaluated": len(graded), "immature": immature,
            "errors": errors}


# ---------- reporting ----------

def _bucket(decisions: list[dict]) -> dict:
    directional = [d for d in decisions if d["hit"] is not None]
    hits = [d for d in directional if d["hit"]]
    rets = [d["fwd_return_pct"] for d in directional if d["fwd_return_pct"] is not None]
    return {
        "count": len(decisions),
        "graded": len(directional),
        "hit_rate": round(len(hits) / len(directional) * 100, 1) if directional else None,
        "avg_fwd_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
    }


def summary(days: int | None = None) -> dict:
    """Activity counts (optionally windowed) plus the all-time track record."""
    since = None
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    events = list_events(since=since, limit=2000)
    by_kind: dict[str, int] = {}
    for e in events:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1

    all_dec = list_decisions(limit=2000)
    recent_dec = [d for d in all_dec if since is None or d["ts"] >= since]
    graded = [d for d in all_dec if d["evaluated_at"]]
    directional_graded = [d for d in graded if d["hit"] is not None]
    flat_graded = [d for d in graded if d["hit"] is None and d["fwd_return_pct"] is not None]

    by_action = {a: _bucket([d for d in graded if d["action"] == a])
                 for a in ACTIONS if any(d["action"] == a for d in all_dec)}
    by_ticker = {}
    for d in graded:
        by_ticker.setdefault(d["ticker"], []).append(d)
    by_ticker = {k: _bucket(v) for k, v in sorted(by_ticker.items())}

    best = max(directional_graded, key=lambda d: d["fwd_return_pct"] * (DIRECTION[d["action"]]),
               default=None)
    worst = min(directional_graded, key=lambda d: d["fwd_return_pct"] * (DIRECTION[d["action"]]),
                default=None)

    def _brief(d):
        if not d:
            return None
        return {k: d[k] for k in ("id", "ts", "ticker", "action", "price", "score",
                                  "fwd_return_pct", "hit")}

    return {
        "window_days": days,
        "events": {"total": len(events), "by_kind": by_kind,
                   "last_ts": events[0]["ts"] if events else None},
        "decisions": {"total": len(all_dec), "in_window": len(recent_dec),
                      "pending": sum(1 for d in all_dec if not d["evaluated_at"]),
                      "graded": len(graded)},
        "track_record": {
            **_bucket(graded),
            "by_action": by_action,
            "by_ticker": by_ticker,
            "flat_calls": {"count": len(flat_graded),
                           "avg_abs_move_pct": round(sum(abs(d["fwd_return_pct"])
                                                         for d in flat_graded) / len(flat_graded), 2)
                           if flat_graded else None},
            "best": _brief(best),
            "worst": _brief(worst),
        },
        "recent_decisions": [{k: d[k] for k in ("id", "ts", "ticker", "action", "horizon_days",
                                                "price", "score", "label", "source",
                                                "fwd_return_pct", "hit")}
                             for d in all_dec[:10]],
    }


# ---------- inter-agent messages ----------
# The bus the agents talk over: the analyst drops briefings/alerts here, the
# trader reads its inbox at the start of every session and every autonomous
# run. Plain rows in the shared DB — durable, auditable, no network needed.

def send_message(from_agent: str, to_agent: str, subject: str,
                 body: str | None = None, kind: str = "briefing",
                 priority: str = "normal", ticker: str | None = None,
                 payload: Any = None, dedupe_hours: float | None = None,
                 ts: str | None = None) -> dict:
    """Deliver a message; with ``dedupe_hours`` an identical recent
    (from, to, kind, subject) is not re-sent — the scanner runs every few
    minutes, and the trader's inbox should not be 40 copies of one story."""
    if not subject or not subject.strip():
        raise ValueError("subject is required")
    if dedupe_hours:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=dedupe_hours)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        with _conn() as c:
            dup = c.execute(
                "SELECT id, ts FROM agent_messages WHERE from_agent = ? AND to_agent = ? "
                "AND kind = ? AND subject = ? AND ts >= ? LIMIT 1",
                (from_agent, to_agent, kind, subject.strip(), cutoff)).fetchone()
        if dup:
            return {"id": dup["id"], "ts": dup["ts"], "subject": subject.strip(),
                    "deduped": True}
    row = {
        "ts": ts or _now(),
        "from_agent": from_agent, "to_agent": to_agent,
        "kind": kind, "priority": priority,
        "ticker": ticker.upper().strip() if ticker else None,
        "subject": subject.strip(), "body": body,
        "payload": _dump(payload),
    }
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO agent_messages (ts, from_agent, to_agent, kind, priority, ticker, "
            "subject, body, payload) VALUES (:ts, :from_agent, :to_agent, :kind, :priority, "
            ":ticker, :subject, :body, :payload)", row)
        row["id"] = cur.lastrowid
    row["payload"] = _load(row["payload"])
    row["deduped"] = False
    return row


def inbox(to_agent: str, unread_only: bool = False, limit: int = 100) -> list[dict]:
    q, args = "SELECT * FROM agent_messages WHERE to_agent = ?", [to_agent]
    if unread_only:
        q += " AND read_at IS NULL"
    q += " ORDER BY CASE priority WHEN 'high' THEN 0 ELSE 1 END, ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["payload"] = _load(r["payload"])
    return rows


def unread_count(to_agent: str) -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM agent_messages WHERE to_agent = ? "
                         "AND read_at IS NULL", (to_agent,)).fetchone()[0]


def mark_read(to_agent: str, ids: list[int] | None = None) -> int:
    """Mark messages read — specific ids, or the whole inbox when ids is None."""
    stamp = _now()
    with _lock, _conn() as c:
        if ids:
            marks = ",".join("?" for _ in ids)
            cur = c.execute(
                f"UPDATE agent_messages SET read_at = ? WHERE to_agent = ? "
                f"AND read_at IS NULL AND id IN ({marks})", [stamp, to_agent, *ids])
        else:
            cur = c.execute("UPDATE agent_messages SET read_at = ? WHERE to_agent = ? "
                            "AND read_at IS NULL", (stamp, to_agent))
        return cur.rowcount


# The agents that report to the Trader, and the event kind each one logs per
# scan. Adding a row here is what puts a new agent on the Neighborhood street.
TOWN_RESIDENTS = {
    "analyst": "news_scan",
    "position_manager": "position_scan",
    "risk_manager": "risk_scan",
    "pattern_engine": "pattern_scan",
}


def agent_status(agent_id: str, scan_kind: str, since: str | None = None) -> dict:
    """What the Neighborhood needs to draw one agent's house.

    Every non-Trader agent answers the same two questions: when did you last
    run and what did you find (``last_scan``), and how much mail have you
    sent since ``since`` (``sent_today``). Keeping this here rather than in
    the route means a new agent joins the street by naming its scan kind,
    and that the wiring is testable without booting the web app.
    """
    scans = list_events(kind=scan_kind, limit=1)
    return {
        "last_scan": scans[0] if scans else None,
        "sent_today": len(messages_sent(agent_id, since=since, limit=1000)),
    }


def messages_sent(from_agent: str, since: str | None = None, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM agent_messages WHERE from_agent = ?", [from_agent]
    if since:
        q += " AND ts >= ?"
        args.append(since)
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["payload"] = _load(r["payload"])
    return rows


# ---------- Robinhood MCP payload ingestion ----------

_WRAPPER_KEYS = ("results", "orders", "positions", "fills", "holdings", "items", "data")
_TICKER_KEYS = ("chain_symbol", "symbol", "ticker", "underlying_symbol", "instrument_symbol")
_QTY_KEYS = ("quantity", "qty", "shares", "processed_quantity")
_PRICE_KEYS = ("average_price", "executed_price", "price", "average_buy_price",
               "limit_price", "stop_price", "mark_price")
_STATE_KEYS = ("state", "status")


def _first(item: dict, keys: tuple[str, ...]):
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return None


def _classify(item: Any, kind_hint: str | None) -> tuple[str, str | None, str | None]:
    """Best-effort (kind, ticker, note) for one payload item.

    Robinhood MCP tool schemas aren't pinned here, so this recognizes common
    broker shapes (orders/fills/positions) by their fields and falls back to
    storing the item untouched under a generic kind — the raw payload is
    always preserved either way.
    """
    if not isinstance(item, dict):
        return (kind_hint or "data", None, None)
    ticker = _first(item, _TICKER_KEYS)
    ticker = str(ticker).upper().strip() if ticker else None
    side = item.get("side") or item.get("direction")
    qty = _first(item, _QTY_KEYS)
    price = _first(item, _PRICE_KEYS)
    state = _first(item, _STATE_KEYS)

    if "average_buy_price" in item or (kind_hint == "positions"):
        kind = "position"
    elif side or "order_id" in item or kind_hint in ("orders", "order"):
        kind = "fill" if str(state or "").lower() == "filled" else "order"
    elif kind_hint in ("fills", "fill"):
        kind = "fill"
    elif price is not None and qty is not None:
        kind = "fill"
    else:
        kind = kind_hint or "data"

    bits = []
    if side:
        bits.append(str(side).lower())
    if qty is not None:
        bits.append(str(qty))
    if ticker:
        bits.append(ticker)
    if item.get("option_type"):
        opt = f"{item.get('strike_price', '?')} {item['option_type']}"
        if item.get("expiration_date"):
            opt += f" {item['expiration_date']}"
        bits.append(opt)
    if price is not None:
        bits.append(f"@ {price}")
    if state:
        bits.append(f"({state})")
    return (kind, ticker, " ".join(str(b) for b in bits) or None)


def _unwrap(payload: Any, kind_hint: str | None,
            _depth: int = 0) -> tuple[list | None, str | None]:
    """Dig through envelope keys for the first list of items.

    Depth is bounded so a pathological payload can't walk forever. A
    wrapper key naming the contents (``positions``) wins the kind hint over
    a generic transport key (``data``), which carries no meaning.
    """
    if _depth > 3 or not isinstance(payload, dict):
        return (None, kind_hint)
    for k in _WRAPPER_KEYS:
        v = payload.get(k)
        if isinstance(v, list):
            return (v, kind_hint or (k if k != "data" else None))
        if isinstance(v, dict):
            items, hint = _unwrap(v, kind_hint, _depth + 1)
            if items is not None:
                return (items, hint)
    return (None, kind_hint)


def ingest(payload: Any, source: str = "robinhood-mcp",
           kind_hint: str | None = None, ts: str | None = None) -> dict:
    """Store a payload collected from a broker/MCP tool as one event per item.

    Accepts a dict, a list, or a JSON string; unwraps *nested* envelopes of
    common keys (results/orders/positions/...). Never raises on unknown
    shapes — worst case the item lands as kind='data' with its raw JSON kept.

    Nesting matters: the Robinhood MCP tools answer with
    ``{"data": {"positions": [...]}}``, so unwrapping a single level would
    store the whole envelope as one unreadable event and leave the Position
    and Risk Managers blind — exactly the failure the discipline in
    CLAUDE.md is written to prevent.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass  # keep the raw string as the payload

    items = payload if isinstance(payload, list) else None
    if items is None:
        items, kind_hint = _unwrap(payload, kind_hint)
    if items is None:
        items = [payload]

    stored, kinds = [], {}
    for item in items:
        kind, ticker, note = _classify(item, kind_hint)
        ev = log_event(kind, ticker=ticker, source=source, note=note, payload=item, ts=ts)
        stored.append(ev["id"])
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"stored": len(stored), "event_ids": stored, "kinds": kinds}


# ---------- export ----------

def export_all() -> dict:
    """Everything, JSON-safe — the backup story for an ephemeral-disk host."""
    return {
        "exported_at": _now(),
        "events": list_events(limit=2000),
        "decisions": list_decisions(limit=2000),
    }
