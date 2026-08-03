"""FastAPI server for the options trading assistant.

Run with:  uvicorn app.main:app --reload   (or ./run.sh)
Then open  http://127.0.0.1:8000
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backtest, data, journal, news, options
from .signals import score_frame, score_series

app = FastAPI(title="Options Trading Assistant")
journal.init()

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
    syms = [s.strip().upper() for s in (tickers or ",".join(data.DEFAULT_WATCHLIST)).split(",") if s.strip()]
    out = []
    for s in syms[:20]:
        try:
            q = data.get_quote(s)
            try:
                hist = data.get_history(s, "6mo", "1d")
                sig = score_frame(hist)
                q["signal_score"] = sig["score"]
                q["signal_label"] = sig["label"]
            except Exception:
                q["signal_score"] = None
                q["signal_label"] = "N/A"
            out.append(q)
        except Exception as e:
            out.append({"ticker": s, "error": str(e)})
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
    """One-call trading brief: signal, options read, backtest stats, news."""
    def run():
        df = data.get_history(ticker, "1y", "1d")
        sig = score_frame(df)
        out = {
            "ticker": ticker.upper(),
            "quote": data.get_quote(ticker),
            "signal": sig,
            "earnings": data.get_earnings_info(ticker),
        }
        try:
            chain = options.analyze_chain(ticker)
            out["options"] = {k: chain[k] for k in (
                "expiry", "dte", "put_call_ratio_volume", "put_call_ratio_oi",
                "atm_iv", "expected_move", "expected_move_pct", "iv_skew",
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
        out["news"] = news.get_ticker_news(ticker, 8)
        out["plan"] = _plan(sig, out)
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


# ---------- Static frontend ----------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _ser(s: pd.Series) -> list:
    return [None if pd.isna(x) else round(float(x), 4) for x in s]


def _safe(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Upstream data error: {e}")
