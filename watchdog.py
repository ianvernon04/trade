#!/usr/bin/env python3
"""Restart the app server when an agent thread stops reporting.

`KeepAlive` in the server's launchd job restarts a *dead process*. That is
not the failure this guards against. On 2026-08-07 the server stayed up and
healthy while the Analyst's thread wedged on a hung RSS feed: the process was
alive, the port was open, `/` returned 200, and three of the four agents kept
scanning. Only the news scans stopped, and nothing noticed for 56 hours.

That mattered because the macro gate reads "no alerts in the window" as
CLEAR. A dead Analyst is indistinguishable from a calm market, so the gate
was answering CLEAR the whole time — not because there was no risk, but
because nothing was looking for any.

Two defences now exist and they do different jobs. The gate's staleness
check makes the *run* fail closed, so a wedged Analyst can never look like
permission to trade. This watchdog tries to make the wedge not happen in the
first place, so the agent isn't merely safe but actually working.

Usage:
    python3 watchdog.py            # check, restart if needed
    python3 watchdog.py --dry-run  # report only, never restart
    python3 watchdog.py --quiet    # print only when it acts

Exit codes: 0 = healthy or repaired, 1 = stale and not repaired.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import tracking  # noqa: E402

SERVER_LABEL = "com.options-trading-assistant.server"
WATCHDOG_SOURCE = "watchdog"

# (agent source, event kind, scan interval, stale threshold) in minutes.
#
# Thresholds are ~4x the scan interval. A scanner that missed one cycle to a
# slow feed is not broken, and restarting the server on that would trade a
# rare 56-hour outage for constant thrash — which costs every agent its
# in-memory state several times an hour.
AGENTS = [
    ("analyst",          "news_scan",     15,  60),
    ("position_manager", "position_scan", 30, 120),
    ("risk_manager",     "risk_scan",     60, 240),
    ("pattern_engine",   "pattern_scan", 240, 960),
]

# Don't restart more often than this, and give up after this many tries in a
# row. A server that cannot come up healthy will not be fixed by restarting
# it a fifth time; past that point the honest move is to stop and say so.
RESTART_COOLDOWN_MIN = 20
MAX_CONSECUTIVE_RESTARTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_agents() -> list[dict]:
    """Per-agent freshness. An agent that never ran counts as stale."""
    out = []
    now = _now()
    for source, kind, interval_min, stale_min in AGENTS:
        last = tracking.agent_last_active(source)
        age_min = None if last is None else (now - _parse(last)).total_seconds() / 60
        out.append({
            "agent": source, "kind": kind, "last": last,
            "age_min": age_min, "stale_after_min": stale_min,
            "stale": age_min is None or age_min > stale_min,
        })
    return out


def recent_restarts(within_min: int = 120) -> list[dict]:
    """Watchdog restarts logged recently, newest first."""
    since = (_now() - timedelta(minutes=within_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    evs = tracking.list_events(kind="note", since=since, limit=200)
    return [e for e in evs
            if (e.get("source") or "") == WATCHDOG_SOURCE
            and "restarted" in (e.get("note") or "").lower()]


def restart_server() -> tuple[bool, str]:
    """`kickstart -k` restarts the job in place; it does not need bootout."""
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True,
                             check=True).stdout.strip()
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{SERVER_LABEL}"],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, "launchctl kickstart -k ok"
        return False, f"kickstart exit {r.returncode}: {(r.stderr or '').strip()}"
    except Exception as exc:  # noqa: BLE001 — a watchdog must not raise
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only; never restart")
    ap.add_argument("--quiet", action="store_true",
                    help="print only when something is wrong or acted on")
    args = ap.parse_args()

    rows = check_agents()
    stale = [r for r in rows if r["stale"]]

    if not args.quiet:
        for r in rows:
            age = "never" if r["age_min"] is None else f"{r['age_min']:.0f}m ago"
            flag = "STALE" if r["stale"] else "ok   "
            print(f"  {flag} {r['agent']:17} {age:>12}  (stale after {r['stale_after_min']}m)")

    if not stale:
        if not args.quiet:
            print("All agents reporting.")
        return 0

    names = ", ".join(r["agent"] for r in stale)

    def _describe(r: dict) -> str:
        if r["age_min"] is None:
            return f"{r['agent']} never ran"
        return f"{r['agent']} {r['age_min']:.0f}m stale"

    detail = "; ".join(_describe(r) for r in stale)
    print(f"STALE: {names}")

    if args.dry_run:
        print("  --dry-run: not restarting.")
        return 1

    prior = recent_restarts(within_min=RESTART_COOLDOWN_MIN)
    if prior:
        print(f"  restarted {len(prior)}x in the last {RESTART_COOLDOWN_MIN}m — "
              "in cooldown, not restarting again.")
        return 1

    streak = recent_restarts(within_min=RESTART_COOLDOWN_MIN * MAX_CONSECUTIVE_RESTARTS)
    if len(streak) >= MAX_CONSECUTIVE_RESTARTS:
        msg = (f"Watchdog giving up: {len(streak)} restarts have not revived "
               f"{names}. The server needs a human.")
        print(f"  {msg}")
        tracking.log_event("note", source=WATCHDOG_SOURCE, note=msg)
        tracking.send_message(
            WATCHDOG_SOURCE, "trader",
            subject="Agent threads down — watchdog gave up",
            body=(f"{detail}. Restarting the server {len(streak)} times did not "
                  "revive them, so the watchdog stopped trying. The macro gate "
                  "fails closed while the Analyst is stale, so runs will stand "
                  "down until this is fixed."),
            kind="alert", priority="high", dedupe_hours=6)
        return 1

    ok, how = restart_server()
    note = (f"Watchdog restarted the app server: {detail}. Result: {how}."
            if ok else
            f"Watchdog FAILED to restart the app server: {detail}. Result: {how}.")
    print(f"  {note}")
    tracking.log_event("note", source=WATCHDOG_SOURCE, note=note)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
