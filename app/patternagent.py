"""The Pattern Engine: agent №5, who learns from history to predict future trades.

Analyzes the agent_decisions table (graded historical calls) to spot repeating
win/loss patterns. Identifies profitable decision contexts (signal strength,
IV percentile, day-of-week, holding period, ticker, option type) and filters
that the Trader can use to tune its recommendations. Sends briefings to the
Trader's inbox about discovered patterns.

Design notes:
- Requires 20+ graded decisions to be useful (statistical minimum).
- analyze_patterns() is a pure function over decision+grade tuples.
- Background loop checks every 4 hours (patterns are slow to emerge; no point
  checking more frequently than new trade grades come in).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from . import tracking

AGENT_ID = "pattern_engine"
TRADER_ID = "trader"
CHECK_INTERVAL = 14400  # seconds (4 hours)

# Pattern detection thresholds.
MIN_DECISIONS = 20  # need at least this many graded calls to find a pattern
MIN_SAMPLE_SIZE = 5  # need at least this many in a bucket (e.g., 5 AAPL trades)
WIN_RATE_THRESHOLD = 0.65  # 65%+ win rate is statistically interesting


def analyze_patterns(decisions: list[dict], grades: list[dict]) -> dict:
    """Analyze decision history for repeating win/loss patterns.

    decisions: list of agent_decisions dicts (ticker, signal_score, iv_percentile, etc.)
    grades: list of agent_grades dicts with (decision_id, hit, forward_return)
    """
    if len(grades) < MIN_DECISIONS:
        return {"status": "insufficient_data", "n_decisions": len(grades), "min_required": MIN_DECISIONS}

    # Map decision ID -> grade.
    grade_map = {g.get("decision_id"): g for g in grades if g.get("decision_id")}

    # Categorize by ticker, signal, IV percentile, direction, etc.
    patterns = {
        "by_ticker": {},
        "by_direction": {},
        "by_iv_regime": {},
        "by_signal_strength": {},
        "by_option_type": {},
        "by_weekday": {},
    }

    for d in decisions:
        decision_id = d.get("id")
        if decision_id not in grade_map:
            continue

        g = grade_map[decision_id]
        hit = g.get("hit")  # None if not directional, True/False if graded
        if hit is None:
            continue

        ticker = d.get("ticker", "unknown")
        direction = d.get("action", "unknown")  # buy, sell, call, put, hold, etc.
        score = d.get("signal_score")
        iv_pct = d.get("iv_percentile")
        option_type = d.get("instrument")  # call, put, stock
        created = d.get("created")
        holding_days = g.get("holding_period_days")

        # By ticker.
        if ticker not in patterns["by_ticker"]:
            patterns["by_ticker"][ticker] = {"wins": 0, "total": 0, "trades": []}
        patterns["by_ticker"][ticker]["wins"] += int(hit)
        patterns["by_ticker"][ticker]["total"] += 1
        patterns["by_ticker"][ticker]["trades"].append(d)

        # By direction (buy/sell/call/put).
        if direction not in patterns["by_direction"]:
            patterns["by_direction"][direction] = {"wins": 0, "total": 0}
        patterns["by_direction"][direction]["wins"] += int(hit)
        patterns["by_direction"][direction]["total"] += 1

        # By IV regime (low/mid/high).
        if iv_pct is not None:
            iv_regime = "low" if iv_pct < 0.33 else "high" if iv_pct > 0.67 else "mid"
            if iv_regime not in patterns["by_iv_regime"]:
                patterns["by_iv_regime"][iv_regime] = {"wins": 0, "total": 0}
            patterns["by_iv_regime"][iv_regime]["wins"] += int(hit)
            patterns["by_iv_regime"][iv_regime]["total"] += 1

        # By signal strength (weak/medium/strong).
        if score is not None:
            strength = "weak" if abs(score) < 20 else "strong" if abs(score) >= 40 else "medium"
            if strength not in patterns["by_signal_strength"]:
                patterns["by_signal_strength"][strength] = {"wins": 0, "total": 0}
            patterns["by_signal_strength"][strength]["wins"] += int(hit)
            patterns["by_signal_strength"][strength]["total"] += 1

        # By option type.
        if option_type and option_type in ("call", "put", "stock"):
            if option_type not in patterns["by_option_type"]:
                patterns["by_option_type"][option_type] = {"wins": 0, "total": 0}
            patterns["by_option_type"][option_type]["wins"] += int(hit)
            patterns["by_option_type"][option_type]["total"] += 1

        # By weekday.
        if created:
            try:
                dt = datetime.fromisoformat(created) if isinstance(created, str) else created
                weekday = dt.strftime("%A")
                if weekday not in patterns["by_weekday"]:
                    patterns["by_weekday"][weekday] = {"wins": 0, "total": 0}
                patterns["by_weekday"][weekday]["wins"] += int(hit)
                patterns["by_weekday"][weekday]["total"] += 1
            except (ValueError, AttributeError):
                pass

    # Extract high-confidence patterns (win rate >= threshold, sample size >= minimum).
    findings = []

    for bucket_type, buckets in patterns.items():
        if bucket_type == "by_ticker":
            continue  # ticker patterns are handled separately below
        for bucket_name, stats in buckets.items():
            if stats["total"] >= MIN_SAMPLE_SIZE:
                win_rate = stats["wins"] / stats["total"]
                if win_rate >= WIN_RATE_THRESHOLD:
                    findings.append({
                        "category": bucket_type,
                        "bucket": bucket_name,
                        "win_rate": round(win_rate, 2),
                        "sample_size": stats["total"],
                        "note": f"{bucket_type.replace('_', ' ').title()}: "
                                f"{bucket_name} trades win {win_rate*100:.0f}% of the time ({stats['wins']}/{stats['total']}).",
                    })

    # Ticker-level patterns (higher bar: needs 5+ trades on the same ticker).
    best_tickers = []
    for ticker, stats in patterns["by_ticker"].items():
        if stats["total"] >= MIN_SAMPLE_SIZE:
            win_rate = stats["wins"] / stats["total"]
            best_tickers.append({
                "ticker": ticker,
                "win_rate": win_rate,
                "sample_size": stats["total"],
            })

    best_tickers.sort(key=lambda x: -x["win_rate"])
    for t in best_tickers[:5]:  # top 5 tickers
        if t["win_rate"] >= WIN_RATE_THRESHOLD:
            findings.append({
                "category": "by_ticker",
                "bucket": t["ticker"],
                "win_rate": t["win_rate"],
                "sample_size": t["sample_size"],
                "note": f"{t['ticker']}: {t['sample_size']} graded calls with {t['win_rate']*100:.0f}% win rate. This is your edge.",
            })

    return {
        "status": "ok",
        "n_decisions": len(grades),
        "findings": findings,
        "patterns": patterns,  # raw data for debugging
    }


def compose_messages(a: dict) -> list[dict]:
    """Turn pattern analysis into inbox messages for the Trader."""
    msgs = []

    if a.get("status") != "ok":
        return []  # not enough data yet

    findings = a.get("findings", [])
    if not findings:
        # No high-confidence patterns found yet.
        return msgs

    for f in findings:
        category = f["category"]
        bucket = f["bucket"]
        win_rate = f["win_rate"]
        sample_size = f["sample_size"]

        if category == "by_ticker":
            msgs.append({
                "kind": "briefing", "priority": "normal", "ticker": bucket,
                "subject": f"Pattern: {bucket} — {win_rate*100:.0f}% win rate",
                "body": f"{bucket} trades have a {win_rate*100:.0f}% historical win rate across "
                        f"{sample_size} graded decisions. Focus on this ticker when the signal is clear.",
                "payload": f, "dedupe_hours": 24,
            })
        elif category == "by_option_type":
            msgs.append({
                "kind": "briefing", "priority": "normal",
                "subject": f"Pattern: {bucket.title()} options — {win_rate*100:.0f}% win rate",
                "body": f"Your {bucket} trades win {win_rate*100:.0f}% of the time ({sample_size} graded calls). "
                        "Bias toward this instrument type when choosing between calls/puts.",
                "payload": f, "dedupe_hours": 24,
            })
        elif category == "by_iv_regime":
            msgs.append({
                "kind": "briefing", "priority": "normal",
                "subject": f"Pattern: {bucket.capitalize()} IV regime — {win_rate*100:.0f}% win rate",
                "body": f"Trades entered in {bucket} IV (IV%: {bucket}) have a {win_rate*100:.0f}% win rate. "
                        f"Favor this regime when choosing entry timing ({sample_size} graded calls).",
                "payload": f, "dedupe_hours": 24,
            })
        elif category == "by_signal_strength":
            msgs.append({
                "kind": "briefing", "priority": "normal",
                "subject": f"Pattern: {bucket.title()} signals — {win_rate*100:.0f}% win rate",
                "body": f"{bucket.title()} signal strength trades win {win_rate*100:.0f}% of the time. "
                        f"Prioritize entries when the signal is in this strength bucket ({sample_size} calls).",
                "payload": f, "dedupe_hours": 24,
            })
        elif category == "by_direction":
            msgs.append({
                "kind": "briefing", "priority": "normal",
                "subject": f"Pattern: {bucket.title()} direction — {win_rate*100:.0f}% win rate",
                "body": f"{bucket.title()} trades have a {win_rate*100:.0f}% hit rate. "
                        "Historically your edge is clearer in this direction.",
                "payload": f, "dedupe_hours": 24,
            })
        elif category == "by_weekday":
            msgs.append({
                "kind": "briefing", "priority": "normal",
                "subject": f"Pattern: {bucket} trades — {win_rate*100:.0f}% win rate",
                "body": f"Trades entered on {bucket}s win {win_rate*100:.0f}% of the time. "
                        f"Your signal may have a day-of-week edge ({sample_size} graded calls).",
                "payload": f, "dedupe_hours": 24,
            })

    # Summary briefing.
    if findings:
        msgs.append({
            "kind": "briefing", "priority": "normal",
            "subject": f"Pattern update: {len(findings)} high-confidence edge(s) found",
            "body": f"Analyzed {a['n_decisions']} graded decisions. "
                    f"Found {len(findings)} repeating pattern(s) with ≥{WIN_RATE_THRESHOLD*100:.0f}% win rate. "
                    "Use these patterns to filter and rank your daily setups.",
            "payload": {"n_findings": len(findings), "n_decisions": a["n_decisions"]},
            "dedupe_hours": 48,
        })

    return msgs


def run_scan(send: bool = True) -> dict:
    """One full Pattern Engine cycle: analyze history, log, deliver insights."""
    from . import journal

    # Fetch all graded decisions from the tracking store.
    decisions = []
    grades = []
    with journal._conn() as c:
        rows = c.execute(
            "SELECT * FROM agent_decisions WHERE hit IS NOT NULL ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            d = dict(row)
            decisions.append(d)
            # Convert to the format expected by analyze_patterns.
            grades.append({
                "decision_id": d["id"],
                "hit": d["hit"],
                "holding_period_days": None,  # not tracked currently
            })

    a = analyze_patterns(decisions, grades)
    tracking.log_event(
        "pattern_scan", source=AGENT_ID,
        note=(f"Analyzed {a.get('n_decisions', 0)} decisions; "
              f"found {len(a.get('findings', []))} high-confidence pattern(s)"),
        payload=a)

    delivered, deduped = [], 0
    if send:
        for m in compose_messages(a):
            r = tracking.send_message(AGENT_ID, TRADER_ID, **m)
            if r.get("deduped"):
                deduped += 1
            else:
                delivered.append({"id": r["id"], "subject": r["subject"],
                                  "priority": m.get("priority", "normal")})

    return {"analysis": a, "delivered": delivered, "deduped": deduped}


def _loop():
    time.sleep(120)  # let the server finish booting first
    while True:
        try:
            run_scan()
        except Exception:
            pass  # fail soft; next cycle retries
        time.sleep(CHECK_INTERVAL)


_started = False


def start_background_thread():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="pattern-engine").start()
