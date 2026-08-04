#!/usr/bin/env python3
"""Health check for the Options Trading Assistant.

Exercises every data path against LIVE data and reports failures. Designed
for a daily pre-market run so problems surface before they matter.

Usage:
    python3 healthcheck.py                    # check the local app + live data
    python3 healthcheck.py --url https://...  # also check a deployed instance
    python3 healthcheck.py --quiet            # only print problems (cron-friendly)

Exit codes: 0 = all good, 1 = failures found (so cron/CI can alert).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

TICKERS = ["NVDA", "AAPL", "SPY"]   # a stock, a mega-cap, an ETF (no earnings)
results: list[tuple[str, str, str]] = []   # (status, name, detail)


OFFLINE = False   # set by the connectivity probe; downgrades data checks to SKIP


def check(name: str, needs_network: bool = True):
    """Decorator: run a check, capture pass/fail/warn without aborting the run.

    When the network is unreachable, data checks are SKIPped rather than
    FAILed — otherwise every offline run looks like a catastrophic outage
    and the daily report cries wolf.
    """
    def wrap(fn):
        if OFFLINE and needs_network:
            results.append(("SKIP", name, "no internet access from this machine"))
            return fn
        start = time.time()
        try:
            detail = fn() or ""
            status = "WARN" if detail.startswith("WARN") else "PASS"
            results.append((status, name, f"{detail} ({time.time() - start:.1f}s)"))
        except Exception as e:
            results.append(("FAIL", name, f"{type(e).__name__}: {e}"))
            if "--trace" in sys.argv:
                traceback.print_exc()
        return fn
    return wrap


def probe_network() -> bool:
    """True if Yahoo Finance is reachable at all."""
    try:
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def assert_finite(value, label: str):
    if value is None:
        raise AssertionError(f"{label} is None")
    if isinstance(value, float) and not math.isfinite(value):
        raise AssertionError(f"{label} is NaN/inf — the JSON-compliance bug is back")
    return value


def no_nan(obj, path="response"):
    """Recursively assert a payload contains no NaN/inf (JSON-breaking)."""
    if isinstance(obj, float) and not math.isfinite(obj):
        raise AssertionError(f"NaN/inf at {path}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            no_nan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            no_nan(v, f"{path}[{i}]")


def run_local_checks():
    from app import backtest, calendar_events, data, options, scanner, scorecard
    from app.signals import score_frame

    @check("Yahoo reachable + price history")
    def _():
        df = data.get_history("NVDA", "6mo", "1d")
        if len(df) < 60:
            return f"WARN: only {len(df)} bars returned"
        if df[["open", "high", "low", "close"]].isna().any().any():
            raise AssertionError("NaN survived get_history")
        return f"{len(df)} clean bars, last close {df['close'].iloc[-1]:.2f}"

    @check("Live quotes (batch)")
    def _():
        q = data.get_quotes_batch(TICKERS)
        missing = [t for t in TICKERS if t not in q]
        if missing:
            return f"WARN: no batch data for {', '.join(missing)}"
        for t, v in q.items():
            assert_finite(v["price"], f"{t} price")
        return ", ".join(f"{t} ${q[t]['price']}" for t in TICKERS)

    @check("Signal engine")
    def _():
        out = []
        for t in TICKERS:
            s = score_frame(data.get_history(t, "1y", "1d"))
            assert_finite(s["close"], f"{t} close")
            assert_finite(s["score"], f"{t} score")
            if not -100 <= s["score"] <= 100:
                raise AssertionError(f"{t} score out of range: {s['score']}")
            if len(s["votes"]) < 5:
                raise AssertionError(f"{t} only {len(s['votes'])} indicator votes")
            out.append(f"{t} {s['score']:+} {s['label']}")
        return "; ".join(out)

    @check("Options chain + greeks + spread")
    def _():
        c = options.analyze_chain("NVDA")
        no_nan(c["recommendation"])
        if not c["calls"] or not c["puts"]:
            raise AssertionError("empty option chain")
        deltas = [r["delta"] for r in c["calls"] if r["delta"] is not None]
        if not deltas:
            raise AssertionError("no greeks computed")
        return (f"{len(c['calls'])}c/{len(c['puts'])}p, exp {c['expiry']}, "
                f"ATM IV {c['atm_iv']}%, bias {c['recommendation']['bias']}")

    @check("Earnings lookup (stock + ETF)")
    def _():
        e = data.get_earnings_info("NVDA")
        etf = data.get_earnings_info("SPY")
        if etf["next_earnings"] is not None:
            raise AssertionError("ETF returned an earnings date — ETF skip-list broke")
        return f"NVDA {e['next_earnings'] or 'none listed'}, SPY correctly skipped"

    @check("Backtest engine")
    def _():
        b = backtest.run_backtest("NVDA", "1y", "1d")
        no_nan(b["trades"])
        return f"{b['num_trades']} trades, win rate {b['win_rate']}%"

    @check("Scorecard")
    def _():
        s = scorecard.signal_scorecard("NVDA")
        return (f"{s['buy_signals']['count']} buy signals, "
                f"{s['buy_signals']['win_rate']}% win rate")

    @check("Scanner")
    def _():
        r = scanner.scan(TICKERS, force=True)
        errs = [s for s in r["setups"] if s.get("error")]
        if errs:
            return "WARN: " + "; ".join(f"{s['ticker']}: {s['error'][:60]}" for s in errs)
        return f"{len(r['setups'])} tickers ranked, top: {r['setups'][0]['ticker']}"

    @check("News feeds")
    def _():
        items = __import__("app.news", fromlist=["news"]).get_tech_news(40)
        if len(items) < 10:
            return f"WARN: only {len(items)} headlines — feeds may be down"
        sources = len({i["source"] for i in items})
        return f"{len(items)} headlines from {sources} sources"

    @check("Macro calendar", needs_network=False)   # pure local date math
    def _():
        ev = calendar_events.upcoming(60)
        if not ev:
            return "WARN: no FOMC/CPI dates in the next 60 days — schedule may need updating for a new year"
        return f"next: {ev[0]['kind']} on {ev[0]['date']} ({ev[0]['in_days']}d)"

    @check("API endpoints (in-process)")
    def _():
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app)
        bad = []
        for path in ["/", "/api/watchlist", "/api/analysis/NVDA", "/api/options/NVDA",
                     "/api/agent/NVDA", "/api/scan", "/api/calendar", "/api/news",
                     "/sw.js", "/static/manifest.webmanifest"]:
            r = c.get(path)
            if r.status_code != 200:
                bad.append(f"{path} -> {r.status_code}")
                continue
            if r.headers.get("content-type", "").startswith("application/json"):
                body = r.text
                if "NaN" in body or "Infinity" in body:
                    bad.append(f"{path} leaked NaN")
                else:
                    json.loads(body)   # must be strict-JSON parseable
        if bad:
            raise AssertionError("; ".join(bad))
        return "10 endpoints OK, all strict-JSON clean"


def run_remote_check(url: str):
    @check(f"Deployed site ({url})")
    def _():
        start = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "healthcheck"})
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read(4000).decode("utf-8", "replace")
            if r.status != 200:
                raise AssertionError(f"HTTP {r.status}")
            if "Options Trading Assistant" not in body:
                raise AssertionError("page loaded but content looks wrong")
        took = time.time() - start
        if took > 25:
            return f"WARN: slow response ({took:.0f}s) — free tier was likely asleep"
        return f"live, responded in {took:.1f}s"

    @check(f"Deployed API ({url}/api/agent/NVDA)")
    def _():
        req = urllib.request.Request(url.rstrip("/") + "/api/agent/NVDA",
                                     headers={"User-Agent": "healthcheck"})
        with urllib.request.urlopen(req, timeout=90) as r:
            payload = json.loads(r.read().decode())
        no_nan(payload)
        degraded = payload.get("degraded") or []
        if degraded:
            return f"WARN: brief rendered but degraded sections: {', '.join(degraded)}"
        return f"agent brief OK — {payload['ticker']} {payload['signal']['label']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="also check a deployed instance")
    ap.add_argument("--quiet", action="store_true", help="print only problems")
    ap.add_argument("--trace", action="store_true", help="print tracebacks on failure")
    args = ap.parse_args()

    print(f"Options Trading Assistant — health check  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    global OFFLINE
    OFFLINE = not probe_network()
    if OFFLINE:
        print("! Yahoo Finance is unreachable from this machine — data checks will be SKIPPED.")
        print("  (Network restriction or Yahoo outage, not necessarily an app fault.)\n")

    run_local_checks()
    if args.url:
        run_remote_check(args.url)

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    skips = [r for r in results if r[0] == "SKIP"]
    passes = [r for r in results if r[0] == "PASS"]
    icons = {"PASS": "✔", "WARN": "!", "FAIL": "✖", "SKIP": "–"}
    for status, name, detail in results:
        if args.quiet and status == "PASS":
            continue
        print(f"{icons[status]} {status:<4} {name:<34} {detail}")

    print("=" * 72)
    summary = f"{len(passes)} passed, {len(warns)} warnings, {len(fails)} failed"
    if skips:
        summary += f", {len(skips)} skipped (offline)"
    print(summary)
    if fails:
        print("\nACTION NEEDED — failing checks:")
        for _, name, detail in fails:
            print(f"  • {name}: {detail}")
    elif not warns and not skips:
        print("\nAll clear — no action needed.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
