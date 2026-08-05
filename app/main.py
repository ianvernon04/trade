"""FastAPI server for the options trading assistant.

Run with:  uvicorn app.main:app --reload   (or ./run.sh)
Then open  http://127.0.0.1:8000
"""

from __future__ import annotations

import base64
import math
import os
import secrets
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (alerts, backtest, calendar_events, data, journal, news, newsagent,
               options, patternagent, positionagent, positions, riskagent, scanner,
               scorecard, tracking)
from .signals import score_frame, score_series


class SafeJSONResponse(JSONResponse):
    """JSON response that tolerates NaN/inf anywhere in the payload.

    Market data legitimately contains gaps (partial trailing bars, holidays,
    indicators before their warm-up period). JSON cannot represent NaN, so
    without this a single missing value aborts an entire response. Every
    endpoint inherits this via the app's default_response_class.
    """

    def render(self, content) -> bytes:
        return super().render(_json_safe(content))


app = FastAPI(title="Options Trading Assistant", default_response_class=SafeJSONResponse)
journal.init()
tracking.init()
alerts.start_background_thread()
newsagent.start_background_thread()
positionagent.start_background_thread()
riskagent.start_background_thread()
patternagent.start_background_thread()

# Optional shared password for hosted/shared deployments. When APP_PASSWORD is
# set, every request must carry HTTP Basic auth with that password (any
# username). Unset = open access, the right default for localhost use.
APP_PASSWORD = os.environ.get("APP_PASSWORD") or None


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    if APP_PASSWORD:
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode()
                supplied = decoded.split(":", 1)[1] if ":" in decoded else ""
                ok = secrets.compare_digest(supplied, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Options Trading Assistant"'},
            )
    return await call_next(request)

STATIC = Path(__file__).resolve().parent / "static"


# ---------- Quotes & watchlist ----------

@app.get("/api/quote/{ticker}")
def quote(ticker: str):
    return _safe(lambda: data.get_quote(ticker))


@app.get("/api/watchlist")
def watchlist(tickers: Optional[str] = None):
    syms = [s.strip().upper() for s in (tickers or ",".join(data.DEFAULT_WATCHLIST)).split(",") if s.strip()][:20]
    # One batched request for all quotes instead of one per ticker — far
    # friendlier to Yahoo's rate limits on shared hosting IPs.
    try:
        batch = data.get_quotes_batch(syms)
    except Exception:
        batch = {}
    out = []
    for s in syms:
        q = batch.get(s)
        if q is None:
            try:
                q = data.get_quote(s)
            except Exception as e:
                out.append({"ticker": s, "error": str(e)})
                continue
        q = dict(q)
        try:
            sig = score_frame(data.get_history(s, "6mo", "1d"))
            q["signal_score"] = sig["score"]
            q["signal_label"] = sig["label"]
        except Exception:
            q["signal_score"] = None
            q["signal_label"] = "N/A"
        out.append(q)
    return {"quotes": out}


# ---------- Analysis (chart + indicators + signal) ----------

@app.get("/api/analysis/{ticker}")
def analysis(ticker: str, period: str = "6mo", interval: str = "1d"):
    def run():
        df = data.get_history(ticker, period, interval)
        sig = score_frame(df)
        scores = score_series(df)
        from .indicators import compute_all
        ind = compute_all(df).tail(260)
        chart = {
            "dates": [str(i)[:16] for i in ind.index],
            "close": _ser(ind["close"]),
            "sma20": _ser(ind["sma20"]),
            "sma50": _ser(ind["sma50"]),
            "bb_upper": _ser(ind["bb_upper"]),
            "bb_lower": _ser(ind["bb_lower"]),
            "rsi": _ser(ind["rsi14"]),
            "macd_hist": _ser(ind["macd_hist"]),
            "volume": _ser(ind["volume"]),
            "score": _ser(scores.tail(260)),
        }
        return {
            "ticker": ticker.upper(),
            "quote": data.get_quote(ticker),
            "signal": sig,
            "chart": chart,
        }
    return _safe(run)


# ---------- Options ----------

@app.get("/api/options/{ticker}")
def options_chain(ticker: str, expiry: Optional[str] = None):
    return _safe(lambda: options.analyze_chain(ticker, expiry))


# ---------- Backtest ----------

@app.get("/api/backtest/{ticker}")
def run_backtest(
    ticker: str,
    period: str = "2y",
    interval: str = "1d",
    buy_threshold: float = Query(20, ge=0, le=90),
    sell_threshold: float = Query(-15, ge=-90, le=0),
    stop_atr: float = Query(1.5, ge=0.5, le=5),
    target_atr: float = Query(3.0, ge=0.5, le=10),
    max_hold: int = Query(15, ge=1, le=100),
    direction: str = "long",
):
    return _safe(lambda: backtest.run_backtest(
        ticker, period, interval, buy_threshold, sell_threshold,
        stop_atr, target_atr, max_hold, direction))


# ---------- Scanner, scorecard, calendar, alerts ----------

@app.get("/api/scan")
def morning_scan(tickers: Optional[str] = None):
    syms = [s.strip().upper() for s in tickers.split(",") if s.strip()] if tickers else None
    return _safe(lambda: scanner.scan(syms))


@app.get("/api/scorecard/{ticker}")
def get_scorecard(ticker: str, horizon: int = Query(10, ge=2, le=60)):
    return _safe(lambda: scorecard.signal_scorecard(ticker, horizon=horizon))


@app.get("/api/calendar")
def macro_calendar(days: int = Query(45, ge=1, le=180)):
    return {"events": calendar_events.upcoming(days),
            "note": "Approximate published schedules — verify at federalreserve.gov / bls.gov."}


@app.get("/api/alerts")
def get_alerts(since_id: int = 0, x_auth_token: str = Header(default="")):
    user = journal.user_for_token(x_auth_token)
    return {"alerts": alerts.list_alerts(since_id, user["id"] if user else None)}


@app.post("/api/alerts/check")
def run_alert_check():
    alerts.generate_alerts()
    return {"ok": True}


# ---------- Optimizer ----------

@app.get("/api/optimize/{ticker}")
def optimize(ticker: str, period: str = "2y", direction: str = "long"):
    return _safe(lambda: backtest.optimize(ticker, period, direction))


# ---------- News ----------

@app.get("/api/news")
def tech_news(limit: int = 80):
    return {"items": news.get_tech_news(limit)}


@app.get("/api/news/{ticker}")
def ticker_news(ticker: str, limit: int = 15):
    return {"items": news.get_ticker_news(ticker, limit)}


# ---------- Agent: everything combined ----------

@app.get("/api/agent/{ticker}")
def agent(ticker: str):
    """One-call trading brief: signal, options read, backtest stats, news.

    Every section degrades independently: if Yahoo rate-limits one call the
    brief still renders with whatever succeeded, rather than failing whole.
    """
    def run():
        df = data.get_history(ticker, "1y", "1d")  # required; everything else optional
        sig = score_frame(df)
        try:
            quote = data.get_quote(ticker)
        except Exception:
            close = sig["close"]
            prev = float(df["close"].iloc[-2]) if len(df) > 1 else close
            quote = {"ticker": ticker.upper(), "price": close, "previous_close": prev,
                     "change": round(close - prev, 4),
                     "change_pct": round((close / prev - 1) * 100, 4) if prev else None,
                     "day_high": None, "day_low": None, "open": None, "volume": None,
                     "market_cap": None, "year_high": None, "year_low": None,
                     "currency": "USD", "as_of": "from daily bars (live quote unavailable)"}
        try:
            earnings = data.get_earnings_info(ticker)
        except Exception:
            earnings = {"next_earnings": None, "days_until": None}
        out = {
            "ticker": ticker.upper(),
            "quote": quote,
            "signal": sig,
            "earnings": earnings,
        }
        try:
            out["confluence"] = scorecard.confluence(sig, scorecard.weekly_signal(ticker))
        except Exception:
            out["confluence"] = None
        try:
            sc = scorecard.signal_scorecard(ticker)
            out["scorecard"] = {"buy": sc["buy_signals"], "sell": sc["sell_signals"],
                                "horizon_days": sc["horizon_days"]}
        except Exception:
            out["scorecard"] = None
        try:
            chain = options.analyze_chain(ticker)
            out["options"] = {k: chain[k] for k in (
                "expiry", "dte", "put_call_ratio_volume", "put_call_ratio_oi",
                "atm_iv", "iv_context", "expected_move", "expected_move_pct", "iv_skew",
                "max_pain", "earnings_before_expiry", "recommendation",
                "unusual_activity")}
        except Exception as e:
            out["options"] = {"error": str(e)}
        try:
            bt = backtest.run_backtest(ticker)
            out["backtest"] = {k: bt[k] for k in (
                "num_trades", "win_rate", "profit_factor", "expectancy_pct",
                "total_return_pct", "buy_hold_return_pct", "max_drawdown_pct")}
        except Exception as e:
            out["backtest"] = {"error": str(e)}
        try:
            out["news"] = news.get_ticker_news(ticker, 8)
        except Exception:
            out["news"] = []
        out["plan"] = _plan(sig, out)
        out["degraded"] = [k for k in ("options", "backtest", "scorecard", "confluence")
                           if not out.get(k) or (isinstance(out.get(k), dict) and out[k].get("error"))]
        return out
    return _safe(run)


def _plan(sig: dict, ctx: dict) -> dict:
    """Turn the combined picture into a concrete, risk-managed plan."""
    close = sig["close"]
    a = sig.get("atr") or close * 0.02
    label = sig["label"]
    rec = (ctx.get("options") or {}).get("recommendation") or {}
    bias = rec.get("bias", "NO TRADE")
    earnings = ctx.get("earnings") or {}
    edays = earnings.get("days_until")
    steps = []
    if bias in ("CALL", "PUT") and label not in ("NEUTRAL",):
        stop = close - 1.5 * a if bias == "CALL" else close + 1.5 * a
        target = close + 3 * a if bias == "CALL" else close - 3 * a
        steps = [
            f"Bias: {bias} ({label}, combined score {rec.get('combined_score')})",
            f"Underlying reference: entry near {close:.2f}, "
            f"invalidation if stock crosses {stop:.2f} (1.5x ATR), target {target:.2f} (3x ATR)",
            "Position size so max premium loss is 1-2% of account",
            "Prefer 30-45 DTE, ~0.35 delta; take partial profits at +50-100% on the option",
        ]
        if edays is not None and edays <= 45:
            steps.append(
                f"⚠ EARNINGS {earnings['next_earnings']} ({edays} days away): plan to be flat "
                "before the report, or accept IV-crush risk deliberately — a correct direction "
                "can still lose money when implied volatility collapses after earnings")
        else:
            steps.append("Exit before major binary events (earnings) unless that IS the thesis")
    else:
        steps = [
            "No edge right now — signal is neutral or options flow disagrees with technicals.",
            "Best trade is often no trade: wait for score to cross +/-20 with volume confirmation.",
        ]

    # Context that matters whether or not there's a trade on.
    conf = ctx.get("confluence")
    if conf:
        steps.append(f"Timeframes: {conf['note'].lower()}")
    sc = ctx.get("scorecard")
    if sc:
        kind = "sell" if bias == "PUT" else "buy"
        side_stats = sc.get(kind)
        if side_stats and side_stats.get("count"):
            steps.append(
                f"Track record: this exact {kind} signal fired {side_stats['count']}x on this "
                f"ticker over 3y with a {side_stats['win_rate']}% win rate over "
                f"{sc['horizon_days']} bars — calibrate your confidence accordingly")
    ivc = (ctx.get("options") or {}).get("iv_context")
    if ivc and ivc["label"] in ("elevated", "extreme"):
        steps.append(f"IV is {ivc['label']} ({ivc['percentile']:.0f}th percentile vs realized) "
                     "— prefer the suggested spread over a naked long option")
    upcoming = calendar_events.upcoming(10)
    if upcoming:
        ev = upcoming[0]
        steps.append(f"Macro: {ev['kind']} in {ev['in_days']} day(s) ({ev['date']}) — "
                     "expect volatility around it")
    return {"bias": bias, "steps": steps,
            "disclaimer": "Educational tool, not financial advice. Options can lose 100% of premium."}


class TradeIn(BaseModel):
    ticker: str
    instrument: str = "call"
    direction: str = "long"
    quantity: float = 1
    strike: Optional[float] = None
    expiry: Optional[str] = None
    entry_price: float
    entry_date: str
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    setup: Optional[str] = None
    notes: Optional[str] = None


class TradeUpdate(BaseModel):
    ticker: Optional[str] = None
    instrument: Optional[str] = None
    direction: Optional[str] = None
    quantity: Optional[float] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    entry_price: Optional[float] = None
    entry_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    setup: Optional[str] = None
    notes: Optional[str] = None


# ---------- Accounts (per-user journals) ----------
# Journal auth uses the X-Auth-Token header (not Authorization) so it can
# coexist with the optional site-wide APP_PASSWORD basic-auth gate.

class AuthIn(BaseModel):
    username: str
    password: str


def current_user(x_auth_token: str = Header(default="")) -> dict:
    user = journal.user_for_token(x_auth_token)
    if not user:
        raise HTTPException(401, "login required")
    return user


@app.post("/api/auth/register")
def auth_register(a: AuthIn):
    try:
        return journal.register(a.username, a.password)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/login")
def auth_login(a: AuthIn):
    try:
        return journal.login(a.username, a.password)
    except ValueError as e:
        raise HTTPException(401, str(e))


@app.post("/api/auth/logout")
def auth_logout(x_auth_token: str = Header(default="")):
    journal.logout(x_auth_token)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(current_user)):
    return user


# ---------- Journal (scoped to the signed-in user) ----------

@app.get("/api/journal")
def journal_list(status: Optional[str] = None, user: dict = Depends(current_user)):
    return {"trades": journal.list_trades(user["id"], status)}


@app.post("/api/journal")
def journal_add(t: TradeIn, user: dict = Depends(current_user)):
    return journal.add_trade(t.model_dump(), user["id"])


@app.patch("/api/journal/{trade_id}")
def journal_update(trade_id: int, t: TradeUpdate, user: dict = Depends(current_user)):
    try:
        return journal.update_trade(trade_id, t.model_dump(exclude_unset=True), user["id"])
    except KeyError:
        raise HTTPException(404, "trade not found")


@app.delete("/api/journal/{trade_id}")
def journal_delete(trade_id: int, user: dict = Depends(current_user)):
    journal.delete_trade(trade_id, user["id"])
    return {"ok": True}


@app.get("/api/journal/stats")
def journal_stats(user: dict = Depends(current_user)):
    return journal.stats(user["id"])


@app.get("/api/journal/insights")
def journal_insights(user: dict = Depends(current_user)):
    return journal.insights(user["id"])


@app.get("/api/positions")
def live_positions(user: dict = Depends(current_user)):
    return positions.live_positions(user["id"])


# ---------- Agent tracking (Claude + Robinhood MCP activity) ----------
# Not scoped per journal user: this is the machine's own activity log, shared
# by the CLI (python -m app ...) and any agent driving the API. The optional
# site-wide APP_PASSWORD gate still applies.

class EventIn(BaseModel):
    kind: str
    ticker: Optional[str] = None
    source: str = "api"
    note: Optional[str] = None
    payload: Any = None


class DecisionIn(BaseModel):
    ticker: str
    action: str
    horizon_days: int = 10
    price: Optional[float] = None
    score: Optional[float] = None
    label: Optional[str] = None
    rationale: Optional[str] = None
    signal: Any = None
    source: str = "api"
    order_id: Optional[str] = None


class IngestIn(BaseModel):
    payload: Any
    source: str = "robinhood-mcp"
    kind: Optional[str] = None


@app.get("/api/tracking/summary")
def tracking_summary(days: Optional[int] = Query(None, ge=1, le=3650)):
    return tracking.summary(days=days)


@app.get("/api/tracking/events")
def tracking_events(ticker: Optional[str] = None, kind: Optional[str] = None,
                    limit: int = Query(100, ge=1, le=2000)):
    return {"events": tracking.list_events(ticker=ticker, kind=kind, limit=limit)}


@app.get("/api/tracking/decisions")
def tracking_decisions(ticker: Optional[str] = None, action: Optional[str] = None,
                       pending: bool = False, limit: int = Query(100, ge=1, le=2000)):
    return {"decisions": tracking.list_decisions(ticker=ticker, action=action,
                                                 pending_only=pending, limit=limit)}


@app.post("/api/tracking/event")
def tracking_add_event(e: EventIn):
    return tracking.log_event(e.kind, ticker=e.ticker, source=e.source,
                              note=e.note, payload=e.payload)


@app.post("/api/tracking/decision")
def tracking_add_decision(d: DecisionIn):
    try:
        return tracking.log_decision(
            d.ticker, d.action, horizon_days=d.horizon_days, price=d.price,
            score=d.score, label=d.label, rationale=d.rationale, signal=d.signal,
            source=d.source, order_id=d.order_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tracking/ingest")
def tracking_ingest(i: IngestIn):
    return tracking.ingest(i.payload, source=i.source, kind_hint=i.kind)


@app.post("/api/tracking/evaluate")
def tracking_evaluate(period: str = "1y"):
    def bars_for(ticker: str):
        df = data.get_history(ticker, period, "1d")
        df = df[df["close"].notna()]
        return [(str(i)[:10], float(c)) for i, c in zip(df.index, df["close"])]
    return _safe(lambda: tracking.evaluate_decisions(bars_for))


@app.get("/api/tracking/export")
def tracking_export():
    return tracking.export_all()


# ---------- Inter-agent messages (the bus the agents talk over) ----------

class MessageIn(BaseModel):
    from_agent: str
    to_agent: str
    subject: str
    body: Optional[str] = None
    kind: str = "note"
    priority: str = "normal"
    ticker: Optional[str] = None
    payload: Any = None


class MarkReadIn(BaseModel):
    agent: str
    ids: Optional[list[int]] = None   # None = whole inbox


@app.get("/api/messages")
def messages_inbox(agent: str = "trader", unread: bool = False,
                   limit: int = Query(50, ge=1, le=1000)):
    return {"messages": tracking.inbox(agent, unread_only=unread, limit=limit),
            "unread": tracking.unread_count(agent)}


@app.post("/api/messages")
def messages_send(m: MessageIn):
    try:
        return tracking.send_message(m.from_agent, m.to_agent, m.subject, body=m.body,
                                     kind=m.kind, priority=m.priority, ticker=m.ticker,
                                     payload=m.payload)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/messages/read")
def messages_mark_read(m: MarkReadIn):
    return {"marked": tracking.mark_read(m.agent, m.ids)}


@app.post("/api/news/scan")
def news_scan_now():
    """Manual Analyst cycle (the background thread also runs one every 15 min)."""
    return _safe(lambda: newsagent.run_scan())


@app.get("/api/town/status")
def town_status():
    """Everything the Neighborhood needs in one local-only call."""
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    scans = tracking.list_events(kind="news_scan", limit=1)
    return {
        "trader": {
            "summary": tracking.summary(),
            "decisions": tracking.list_decisions(limit=30),
            "unread_mail": tracking.unread_count("trader"),
        },
        "analyst": {
            "last_scan": scans[0] if scans else None,
            "sent_today": len(tracking.messages_sent("analyst", since=today, limit=1000)),
        },
    }


# ---------- Static frontend ----------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/sw.js")
def service_worker():
    # Served from the root so the service worker's scope covers the whole app.
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _ser(s: pd.Series) -> list:
    return [None if pd.isna(x) else round(float(x), 4) for x in s]


def _json_safe(obj):
    """Recursively replace NaN/inf with None.

    JSON has no NaN, so a single missing price bar anywhere in a payload
    would otherwise abort the whole response mid-serialization. This is the
    last line of defence behind the per-module guards.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        v = obj.item()
        return v if not isinstance(v, float) or math.isfinite(v) else None
    return obj


def _safe(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "429" in msg or "rate" in low and "limit" in low or "too many requests" in low:
            raise HTTPException(
                503, "Yahoo Finance is rate-limiting this server right now (shared hosting IPs "
                     "get throttled). Wait ~60 seconds and try again — cached data still works.")
        if "curl" in low or "connect" in low or "timed out" in low or "timeout" in low:
            raise HTTPException(
                503, f"Couldn't reach Yahoo Finance: {msg}. Check the server's internet access, "
                     "then retry.")
        raise HTTPException(502, f"Upstream data error: {msg}")
