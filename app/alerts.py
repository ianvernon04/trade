"""Alert engine.

A background thread re-scans the watchlist every few minutes and writes
alerts into SQLite when something actionable happens:

- a watchlist ticker's signal crosses the buy/sell threshold (broadcast)
- an open journal position is close to expiry or earnings (per-user)

The frontend polls /api/alerts?since_id=N and shows browser notifications.
Alerts are deduped per (ticker, kind, day) so a condition fires once a day.
"""

from __future__ import annotations

import threading
import time

from . import data, journal, scanner

CHECK_INTERVAL = 300  # seconds


def _add(kind: str, message: str, ticker: str | None = None, user_id: int | None = None):
    with journal._lock, journal._conn() as c:
        dup = c.execute(
            "SELECT 1 FROM alerts WHERE kind = ? AND ifnull(ticker,'') = ifnull(?,'') "
            "AND ifnull(user_id,-1) = ifnull(?,-1) AND day = date('now')",
            (kind, ticker, user_id)).fetchone()
        if dup:
            return
        c.execute("INSERT INTO alerts (user_id, ticker, kind, message) VALUES (?, ?, ?, ?)",
                  (user_id, ticker, kind, message))


def list_alerts(since_id: int = 0, user_id: int | None = None, limit: int = 50) -> list[dict]:
    with journal._conn() as c:
        rows = c.execute(
            "SELECT * FROM alerts WHERE id > ? AND (user_id IS NULL OR user_id = ?) "
            "ORDER BY id DESC LIMIT ?", (since_id, user_id if user_id is not None else -1, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def generate_alerts():
    """One pass of alert generation. Called by the background loop, and safe
    to call ad hoc; every data fetch fails soft."""
    # --- watchlist signal crossings (broadcast) ---
    try:
        result = scanner.scan()
        for s in result["setups"]:
            if s.get("error"):
                continue
            if s.get("crossed_buy"):
                _add("signal_buy",
                     f"{s['ticker']} signal crossed BUY (+{s['score']}, {s['label']}; "
                     f"weekly {s['weekly_label']})", s["ticker"])
            if s.get("crossed_sell"):
                _add("signal_sell",
                     f"{s['ticker']} signal crossed SELL ({s['score']}, {s['label']}; "
                     f"weekly {s['weekly_label']})", s["ticker"])
            if s.get("score") is not None and abs(s["score"]) >= 40:
                _add("signal_strong",
                     f"{s['ticker']} at {s['label']} ({s['score']:+}) — strongest reading "
                     "on the watchlist scale", s["ticker"])
    except Exception:
        pass

    # --- open-position risk (per user; cheap checks only, no chain fetches) ---
    try:
        from datetime import date
        with journal._conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM trades WHERE status = 'open' AND user_id IS NOT NULL").fetchall()]
        today = date.today()
        for t in rows:
            uid = t["user_id"]
            if t.get("expiry"):
                try:
                    dte = (date.fromisoformat(t["expiry"]) - today).days
                    if 0 <= dte <= 5:
                        _add("position_expiry",
                             f"Open {t['ticker']} {t['instrument']} expires in {dte} day(s) "
                             f"({t['expiry']}) — theta decay is steepest now; close or roll",
                             t["ticker"], uid)
                    elif dte < 0:
                        _add("position_expired",
                             f"Open {t['ticker']} {t['instrument']} expired {t['expiry']} — "
                             "record the exit in your journal", t["ticker"], uid)
                except ValueError:
                    pass
            try:
                e = data.get_earnings_info(t["ticker"])
                if e.get("days_until") is not None and e["days_until"] <= 3:
                    _add("position_earnings",
                         f"{t['ticker']} reports earnings {e['next_earnings']} "
                         f"({e['days_until']} day(s)) and you have an open position — "
                         "decide now: close before the report or accept IV-crush risk",
                         t["ticker"], uid)
            except Exception:
                pass
    except Exception:
        pass


def _loop():
    time.sleep(30)  # let the server finish booting first
    while True:
        generate_alerts()
        time.sleep(CHECK_INTERVAL)


_started = False


def start_background_thread():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="alert-engine").start()
