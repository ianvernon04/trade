"""The Analyst: agent №2, who reads the news so the Trader doesn't have to.

Continuously scans the app's merged RSS headlines (news.py), aggregates
sentiment per ticker, spots same-direction headline clusters, and watches
for macro-risk topics (Fed/rates, inflation prints, tariffs, geopolitics…).
Findings travel to the Trader over the shared message bus
(tracking.agent_messages): routine digests as normal-priority briefings,
clusters and macro topics as alerts. The Trader reads its inbox at the start
of every session and every autonomous run — separation of duties: the
Analyst informs, the Trader decides, and the Analyst never touches broker
tools.

Design notes:
- analyze()/compose_messages() are pure functions over headline dicts so
  tests inject fixtures; run_scan() accepts a fetch callable for the same
  reason. Only the background loop and the default fetch touch the network.
- Message dedupe (subject-stable, windowed) keeps a 15-minute scan cadence
  from flooding the inbox with 40 copies of one story.
"""

from __future__ import annotations

import re
import threading
import time

from . import tracking

AGENT_ID = "analyst"
TRADER_ID = "trader"
CHECK_INTERVAL = 900  # seconds between scans while the server runs

# Broad-market risk topics. Matched against headline text, case-insensitive.
MACRO_PATTERNS = {
    "fed/rates": r"\b(fed|fomc|powell|rate (hike|cut)s?|interest rates?)\b",
    "inflation": r"\b(cpi|ppi|inflation)\b",
    "jobs": r"\b(jobs report|nonfarm|payrolls|unemployment rate)\b",
    "tariffs/trade": r"\b(tariffs?|trade war|export (bans?|controls?)|sanctions?)\b",
    "geopolitics": r"\b(war|invasion|missile|airstrikes?|escalation)\b",
    "credit/banks": r"\b(defaults?|bank (run|failure|collapse)|credit crunch|bailout)\b",
}
_MACRO_RE = {k: re.compile(p, re.I) for k, p in MACRO_PATTERNS.items()}

# Cluster thresholds: how many same-direction headlines on one ticker turn
# into an alert rather than a digest line.
CLUSTER_MIN = 3


def analyze(items: list[dict]) -> dict:
    """Aggregate a batch of headline dicts (news.get_tech_news shape)."""
    per: dict[str, dict] = {}
    macro: list[dict] = []
    pos = neg = 0

    for it in items:
        s = it.get("sentiment", "neutral")
        if s == "positive":
            pos += 1
        elif s == "negative":
            neg += 1
        for t in it.get("tickers") or []:
            d = per.setdefault(t, {"mentions": 0, "positive": 0, "negative": 0,
                                   "neutral": 0, "headlines": []})
            d["mentions"] += 1
            d[s] += 1
            if len(d["headlines"]) < 4:
                d["headlines"].append(it.get("title", ""))

    for topic, rx in _MACRO_RE.items():
        hits = [it.get("title", "") for it in items if rx.search(it.get("title", ""))]
        if hits:
            macro.append({"topic": topic, "count": len(hits), "headlines": hits[:3]})
    macro.sort(key=lambda m: -m["count"])

    for t, d in per.items():
        d["net"] = d["positive"] - d["negative"]

    rated = pos + neg
    net_ratio = (pos - neg) / rated if rated else 0.0
    if rated >= 8 and net_ratio >= 0.3:
        tone = "bullish"
    elif rated >= 8 and net_ratio <= -0.3:
        tone = "bearish"
    else:
        tone = "mixed"

    return {
        "n_headlines": len(items),
        "overall": {"positive": pos, "negative": neg, "net_ratio": round(net_ratio, 3),
                    "sentiment": tone},
        "tickers": dict(sorted(per.items(), key=lambda kv: -kv[1]["mentions"])),
        "macro": macro,
    }


def compose_messages(a: dict) -> list[dict]:
    """Turn an analysis into inbox-ready messages (subjects kept stable so
    windowed dedupe works; volatile numbers live in the body)."""
    msgs: list[dict] = []
    ov = a["overall"]

    for m in a["macro"]:
        if m["count"] < 2:
            continue
        msgs.append({
            "kind": "alert", "priority": "high",
            "subject": f"Macro risk in headlines: {m['topic']}",
            "body": (f"{m['count']} headline(s) on {m['topic']} this scan. "
                     "Weigh before any new position: "
                     + " | ".join(m["headlines"])),
            "payload": m, "dedupe_hours": 12,
        })

    for t, d in a["tickers"].items():
        if d["negative"] >= CLUSTER_MIN and d["negative"] >= 2 * d["positive"]:
            msgs.append({
                "kind": "alert", "priority": "high", "ticker": t,
                "subject": f"Negative news cluster: {t}",
                "body": (f"{d['negative']} negative vs {d['positive']} positive "
                         f"headline(s) on {t}: " + " | ".join(d["headlines"])),
                "payload": d, "dedupe_hours": 12,
            })
        elif d["positive"] >= CLUSTER_MIN and d["positive"] >= 2 * d["negative"]:
            msgs.append({
                "kind": "briefing", "priority": "normal", "ticker": t,
                "subject": f"Positive news cluster: {t}",
                "body": (f"{d['positive']} positive vs {d['negative']} negative "
                         f"headline(s) on {t}: " + " | ".join(d["headlines"])),
                "payload": d, "dedupe_hours": 12,
            })

    top = list(a["tickers"].items())[:5]
    digest_lines = [f"{ov['positive']} positive / {ov['negative']} negative across "
                    f"{a['n_headlines']} headlines (net {ov['net_ratio']:+.2f})."]
    if top:
        digest_lines.append("Most covered: " + ", ".join(
            f"{t} ({d['mentions']}, net {d['net']:+d})" for t, d in top))
    if a["macro"]:
        digest_lines.append("Macro topics: " + ", ".join(
            f"{m['topic']}×{m['count']}" for m in a["macro"]))
    msgs.append({
        "kind": "briefing", "priority": "normal",
        "subject": f"Market news digest — {ov['sentiment']} tone",
        "body": " ".join(digest_lines),
        "payload": {"overall": ov, "macro": a["macro"]},
        "dedupe_hours": 6,
    })
    return msgs


def run_scan(fetch=None, send: bool = True) -> dict:
    """One full Analyst cycle: read feeds, analyze, log, deliver mail."""
    if fetch is None:
        from . import news
        fetch = news.get_tech_news
    items = fetch(120) or []
    a = analyze(items)
    tracking.log_event(
        "news_scan", source=AGENT_ID,
        note=(f"{a['n_headlines']} headlines — {a['overall']['sentiment']} tone; "
              f"{len(a['macro'])} macro topic(s), {len(a['tickers'])} ticker(s) tagged"),
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
    time.sleep(45)  # let the server finish booting first
    while True:
        try:
            run_scan()
        except Exception:
            pass  # feeds fail soft; next cycle retries
        time.sleep(CHECK_INTERVAL)


_started = False


def start_background_thread():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="news-analyst").start()
