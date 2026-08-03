"""Trade journal: SQLite-backed log of entries/exits with P&L analytics.

Multi-user: each journal row belongs to a user account. Accounts are
lightweight username+password pairs stored locally (PBKDF2-hashed);
session tokens live in the same database so logins survive restarts.
"""

from __future__ import annotations

import hashlib
import re
import secrets
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
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
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
        cols = [r[1] for r in c.execute("PRAGMA table_info(trades)")]
        if "user_id" not in cols:  # migrate pre-account databases
            c.execute("ALTER TABLE trades ADD COLUMN user_id INTEGER")


# ---------- accounts ----------

def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def register(username: str, password: str) -> dict:
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,30}", username):
        raise ValueError("username must be 2-30 letters, digits, or _ . -")
    if len(password or "") < 4:
        raise ValueError("password must be at least 4 characters")
    salt = secrets.token_hex(16)
    with _lock, _conn() as c:
        first_user = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        try:
            cur = c.execute(
                "INSERT INTO users (username, pw_salt, pw_hash) VALUES (?, ?, ?)",
                (username, salt, _hash(password, salt)))
        except sqlite3.IntegrityError:
            raise ValueError("that username is already taken")
        uid = cur.lastrowid
        if first_user:
            # Trades logged before accounts existed belong to the first account.
            c.execute("UPDATE trades SET user_id = ? WHERE user_id IS NULL", (uid,))
        token = secrets.token_hex(32)
        c.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, uid))
    return {"token": token, "username": username}


def login(username: str, password: str) -> dict:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?",
                        ((username or "").strip(),)).fetchone()
        if not row or not secrets.compare_digest(
                row["pw_hash"], _hash(password or "", row["pw_salt"])):
            raise ValueError("invalid username or password")
        token = secrets.token_hex(32)
        c.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)",
                  (token, row["id"]))
        return {"token": token, "username": row["username"]}


def logout(token: str):
    with _lock, _conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def user_for_token(token: str) -> dict | None:
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT u.id, u.username FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token = ?", (token,)).fetchone()
        return dict(row) if row else None


# ---------- trades (always scoped to a user) ----------

def _pnl(row: dict) -> float | None:
    if row["exit_price"] is None:
        return None
    sign = 1 if row["direction"] == "long" else -1
    mult = MULT.get(row["instrument"], 100)
    return round(sign * (row["exit_price"] - row["entry_price"]) * row["quantity"] * mult, 2)


def add_trade(t: dict, user_id: int) -> dict:
    fields = ["ticker", "instrument", "direction", "quantity", "strike", "expiry",
              "entry_price", "entry_date", "exit_price", "exit_date", "setup", "notes"]
    values = {f: t.get(f) for f in fields}
    values["ticker"] = (values["ticker"] or "").upper().strip()
    values["status"] = "closed" if values.get("exit_price") is not None else "open"
    values["user_id"] = user_id
    with _lock, _conn() as c:
        cur = c.execute(
            f"INSERT INTO trades ({', '.join(values)}) "
            f"VALUES ({', '.join(':' + k for k in values)})",
            values,
        )
        return get_trade(cur.lastrowid, user_id, conn=c)


def update_trade(trade_id: int, t: dict, user_id: int) -> dict:
    allowed = ["ticker", "instrument", "direction", "quantity", "strike", "expiry",
               "entry_price", "entry_date", "exit_price", "exit_date", "setup", "notes"]
    updates = {k: v for k, v in t.items() if k in allowed}
    if not updates:
        return get_trade(trade_id, user_id)
    if "exit_price" in updates:
        updates["status"] = "closed" if updates["exit_price"] is not None else "open"
    sets = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = trade_id
    updates["user_id"] = user_id
    with _lock, _conn() as c:
        c.execute(f"UPDATE trades SET {sets} WHERE id = :id AND user_id = :user_id", updates)
        return get_trade(trade_id, user_id, conn=c)


def delete_trade(trade_id: int, user_id: int):
    with _lock, _conn() as c:
        c.execute("DELETE FROM trades WHERE id = ? AND user_id = ?", (trade_id, user_id))


def get_trade(trade_id: int, user_id: int, conn: sqlite3.Connection | None = None) -> dict:
    c = conn or _conn()
    row = c.execute("SELECT * FROM trades WHERE id = ? AND user_id = ?",
                    (trade_id, user_id)).fetchone()
    if conn is None:
        c.close()
    if not row:
        raise KeyError(f"trade {trade_id} not found")
    d = dict(row)
    d["pnl"] = _pnl(d)
    return d


def list_trades(user_id: int, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM trades WHERE user_id = ?"
    args: list = [user_id]
    if status in ("open", "closed"):
        q += " AND status = ?"
        args.append(status)
    q += " ORDER BY entry_date DESC, id DESC"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    for r in rows:
        r["pnl"] = _pnl(r)
    return rows


def stats(user_id: int) -> dict:
    closed = [t for t in list_trades(user_id, "closed") if t["pnl"] is not None]
    open_trades = list_trades(user_id, "open")
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
