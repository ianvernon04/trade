"""Trade journal: SQLite-backed log of entries/exits with P&L analytics."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "journal.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    instrument TEXT NOT NULL DEFAULT 'call',      -- call | put | stock
    direction TEXT NOT NULL DEFAULT 'long',        -- long | short
    quantity REAL NOT NULL DEFAULT 1,              -- contracts or shares
    strike REAL,
    expiry TEXT,
    entry_price REAL NOT NULL,
    entry_date TEXT NOT NULL,
    exit_price REAL,
    exit_date TEXT,
    status TEXT NOT NULL DEFAULT 'open',           -- open | closed
    setup TEXT,                                     -- what signal triggered it
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

MULT = {"call": 100, "put": 100, "stock": 1}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


def _pnl(row: dict) -> float | None:
    if row["exit_price"] is None:
        return None
    sign = 1 if row["direction"] == "long" else -1
    mult = MULT.get(row["instrument"], 100)
    return round(sign * (row["exit_price"] - row["entry_price"]) * row["quantity"] * mult, 2)


def add_trade(t: dict) -> dict:
    fields = ["ticker", "instrument", "direction", "quantity", "strike", "expiry",
              "entry_price", "entry_date", "exit_price", "exit_date", "setup", "notes"]
    values = {f: t.get(f) for f in fields}
    values["ticker"] = (values["ticker"] or "").upper().strip()
    values["status"] = "closed" if values.get("exit_price") is not None else "open"
    with _lock, _conn() as c:
        cur = c.execute(
            f"INSERT INTO trades ({', '.join(values)}, status) "
            f"VALUES ({', '.join(':' + k for k in values)}, :status)",
            values,
        )
        return get_trade(cur.lastrowid, conn=c)


def update_trade(trade_id: int, t: dict) -> dict:
    allowed = ["ticker", "instrument", "direction", "quantity", "strike", "expiry",
               "entry_price", "entry_date", "exit_price", "exit_date", "setup", "notes"]
    updates = {k: v for k, v in t.items() if k in allowed}
    if not updates:
        return get_trade(trade_id)
    if "exit_price" in updates:
        updates["status"] = "closed" if updates["exit_price"] is not None else "open"
    sets = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = trade_id
    with _lock, _conn() as c:
        c.execute(f"UPDATE trades SET {sets} WHERE id = :id", updates)
        return get_trade(trade_id, conn=c)


def delete_trade(trade_id: int):
    with _lock, _conn() as c:
        c.execute("DELETE FROM trades WHERE id = ?", (trade_id,))


def get_trade(trade_id: int, conn: sqlite3.Connection | None = None) -> dict:
    c = conn or _conn()
    row = c.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if conn is None:
        c.close()
    if not row:
        raise KeyError(f"trade {trade_id} not found")
    d = dict(row)
    d["pnl"] = _pnl(d)
    return d


def list_trades(status: str | None = None) -> list[dict]:
    q = "SELECT * FROM trades"
    args: tuple = ()
    if status in ("open", "closed"):
        q += " WHERE status = ?"
        args = (status,)
    q += " ORDER BY entry_date DESC, id DESC"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["pnl"] = _pnl(r)
    return rows


def stats() -> dict:
    closed = [t for t in list_trades("closed") if t["pnl"] is not None]
    open_trades = list_trades("open")
    pnls = [t["pnl"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # Cumulative P&L over time for the equity chart.
    curve, total = [], 0.0
    for t in sorted(closed, key=lambda x: (x["exit_date"] or "", x["id"])):
        total += t["pnl"]
        curve.append({"date": t["exit_date"], "cum_pnl": round(total, 2)})

    by_ticker: dict[str, float] = {}
    for t in closed:
        by_ticker[t["ticker"]] = round(by_ticker.get(t["ticker"], 0) + t["pnl"], 2)

    return {
        "total_trades": len(closed),
        "open_trades": len(open_trades),
        "total_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": (round(sum(wins) / -sum(losses), 2)
                          if losses and sum(losses) != 0 else None),
        "best_trade": max(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "pnl_by_ticker": by_ticker,
        "equity_curve": curve,
    }
