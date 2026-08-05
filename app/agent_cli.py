"""Agent console: analyze, record, ingest, grade, report — from the terminal.

This is the interface Claude Code sessions use (see CLAUDE.md) to pair the
Robinhood MCP server with this app's own indicators, and to leave a paper
trail: every analysis, recommendation, and broker action lands in the
tracking tables (app/tracking.py) inside journal.db.

    python -m app analyze NVDA --options     signal brief, records a decision
    python -m app decide --ticker NVDA --action call --price 181.2 ...
    python -m app log order --source robinhood-mcp --payload '<tool JSON>'
    python -m app ingest --payload @positions.json
    python -m app evaluate                   grade matured decisions
    python -m app report                     activity + track record
    python -m app export --out backup.json

Market-data imports (pandas/yfinance) happen inside the commands that need
them, so logging, reporting, and exporting all work offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import autonomy, tracking

# Signal labels map to recordable actions; an options recommendation can
# upgrade buy/sell to call/put (see cmd_analyze).
_LABEL_ACTION = {"STRONG BUY": "buy", "BUY": "buy", "SELL": "sell",
                 "STRONG SELL": "sell", "NEUTRAL": "hold"}


def _out(data, as_json: bool, human):
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        human(data)


def _payload_from(arg: str | None):
    """--payload accepts inline JSON, @file, or '-' for stdin."""
    if arg is None:
        return None
    if arg == "-":
        text = sys.stdin.read()
    elif arg.startswith("@"):
        text = Path(arg[1:]).read_text()
    else:
        text = arg
    try:
        return json.loads(text)
    except ValueError:
        return text.strip()  # tolerate non-JSON: raw text is still evidence


# ---------- analyze ----------

def cmd_analyze(args) -> int:
    from . import data
    from .signals import score_frame

    df = data.get_history(args.ticker, args.period, "1d")
    sig = score_frame(df)
    out = {"ticker": args.ticker.upper(), "signal": sig}

    try:
        out["quote"] = data.get_quote(args.ticker)
    except Exception:
        out["quote"] = {"price": sig["close"], "note": "live quote unavailable, using last close"}
    try:
        out["earnings"] = data.get_earnings_info(args.ticker)
    except Exception:
        out["earnings"] = None

    if args.weekly:
        try:
            from . import scorecard
            out["confluence"] = scorecard.confluence(sig, scorecard.weekly_signal(args.ticker))
        except Exception as e:
            out["confluence"] = {"error": str(e)}

    action = _LABEL_ACTION.get(sig["label"], "hold")
    if args.options:
        try:
            from . import options
            chain = options.analyze_chain(args.ticker)
            rec = chain.get("recommendation") or {}
            out["options"] = {k: chain.get(k) for k in (
                "expiry", "dte", "atm_iv", "iv_context", "expected_move_pct",
                "put_call_ratio_volume", "max_pain", "earnings_before_expiry")}
            out["options"]["recommendation"] = rec
            if rec.get("bias") in ("CALL", "PUT") and action != "hold":
                action = rec["bias"].lower()
        except Exception as e:
            out["options"] = {"error": str(e)}

    top = sorted(sig["votes"], key=lambda v: abs(v["score"]), reverse=True)[:2]
    rationale = "; ".join(v["reason"] for v in top)

    if not args.no_record:
        decision = tracking.log_decision(
            args.ticker, action, horizon_days=args.horizon,
            price=(out["quote"] or {}).get("price") or sig["close"],
            score=sig["score"], label=sig["label"], rationale=rationale,
            signal=sig, source="agent-cli")
        out["decision"] = {"id": decision["id"], "action": action,
                           "horizon_days": args.horizon}

    def human(o):
        q = o.get("quote") or {}
        chg = q.get("change_pct")
        print(f"{o['ticker']}  price {q.get('price')}"
              + (f" ({chg:+.2f}%)" if isinstance(chg, (int, float)) else ""))
        s = o["signal"]
        print(f"Signal: {s['score']:+.1f} {s['label']}   RSI {s['rsi']}  ATR {s['atr']}")
        for v in s["votes"]:
            print(f"  {v['component']:<11}{v['score']:>+7.1f}  {v['reason']}")
        e = o.get("earnings") or {}
        if e.get("next_earnings"):
            print(f"Earnings: {e['next_earnings']} ({e.get('days_until')} days)")
        c = o.get("confluence")
        if c and not c.get("error"):
            print(f"Weekly: {c['weekly_score']:+.1f} {c['weekly_label']} — {c['note']}")
        op = o.get("options")
        if op and not op.get("error"):
            rec = op.get("recommendation") or {}
            ivc = op.get("iv_context") or {}
            print(f"Options: {rec.get('bias', '—')} (combined {rec.get('combined_score')}) | "
                  f"IV {op.get('atm_iv')} ({ivc.get('label', '?')}) | "
                  f"expected move {op.get('expected_move_pct')}% by {op.get('expiry')}")
        elif op:
            print(f"Options: unavailable ({op['error']})")
        d = o.get("decision")
        if d:
            print(f"Recorded decision #{d['id']}: {d['action']} "
                  f"(graded on the {d['horizon_days']}-bar move; run `evaluate` later)")

    _out(out, args.json, human)
    return 0


# ---------- decide / log / ingest ----------

def cmd_decide(args) -> int:
    price, score, label, signal = args.price, args.score, args.label, None
    if args.snapshot:
        try:
            from . import data
            from .signals import score_frame
            sig = score_frame(data.get_history(args.ticker, "6mo", "1d"))
            signal = sig
            score = score if score is not None else sig["score"]
            label = label or sig["label"]
            price = price if price is not None else sig["close"]
        except Exception as e:
            print(f"note: signal snapshot unavailable ({e}); recording without it",
                  file=sys.stderr)
    d = tracking.log_decision(
        args.ticker, args.action, horizon_days=args.horizon, price=price,
        score=score, label=label, rationale=args.rationale, signal=signal,
        source=args.source, order_id=args.order_id)
    _out(d, args.json, lambda o: print(
        f"Recorded decision #{o['id']}: {o['action']} {o['ticker']}"
        + (f" @ {o['price']}" if o["price"] is not None else "")))
    return 0


def cmd_log(args) -> int:
    ev = tracking.log_event(args.kind, ticker=args.ticker, source=args.source,
                            note=args.note, payload=_payload_from(args.payload))
    _out(ev, args.json, lambda o: print(
        f"Logged event #{o['id']}: {o['kind']}"
        + (f" {o['ticker']}" if o["ticker"] else "")
        + (f" — {o['note']}" if o["note"] else "")))
    return 0


def cmd_ingest(args) -> int:
    payload = _payload_from(args.payload)
    if payload is None:
        print("error: provide --payload (inline JSON, @file, or '-' for stdin)",
              file=sys.stderr)
        return 2
    res = tracking.ingest(payload, source=args.source, kind_hint=args.kind)
    _out(res, args.json, lambda o: print(
        f"Stored {o['stored']} event(s): "
        + ", ".join(f"{k}×{n}" for k, n in o["kinds"].items())))
    return 0


# ---------- scan (headless-friendly market sweep, for autonomous mode) ----------

def cmd_scan(args) -> int:
    from . import scanner
    syms = [s.strip().upper() for s in args.tickers.split(",") if s.strip()] if args.tickers else None
    d = scanner.scan(syms)

    def human(o):
        print(f"Scan generated {o['generated_at']}")
        for s in o["setups"]:
            if s.get("error"):
                print(f"  ! {s['ticker']}: {s['error']}")
                continue
            flags = []
            if s["crossed_buy"]:
                flags.append("BUY-cross")
            if s["crossed_sell"]:
                flags.append("SELL-cross")
            if s["confluence"] == "agree":
                flags.append("weekly-agrees")
            if s["earnings_in_days"] is not None and s["earnings_in_days"] <= 7:
                flags.append(f"earnings-{s['earnings_in_days']}d")
            print(f"  {s['ticker']:<6} {s['score']:+6.1f} {s['label']:<12} "
                  f"rank {s['rank']:>5.1f}  {' '.join(flags)}")
        print(o["note"])

    _out(d, args.json, human)
    return 0


# ---------- evaluate / report / lists / export ----------

def cmd_evaluate(args) -> int:
    from . import data

    def bars_for(ticker: str):
        df = data.get_history(ticker, args.period, "1d")
        df = df[df["close"].notna()]
        return [(str(i)[:10], float(c)) for i, c in zip(df.index, df["close"])]

    res = tracking.evaluate_decisions(bars_for)

    def human(o):
        print(f"Graded {o['evaluated']} decision(s); {o['immature']} not matured yet.")
        for d in o["graded"]:
            mark = "✓ hit" if d["hit"] == 1 else "✗ miss" if d["hit"] == 0 else "· flat call"
            print(f"  #{d['id']} {d['ts'][:10]} {d['ticker']:<6} {d['action']:<8} "
                  f"{d['fwd_return_pct']:+.2f}% over {d['horizon_days']} bars  {mark}")
        for t, err in o["errors"].items():
            print(f"  ! {t}: {err}")

    _out(res, args.json, human)
    return 0


def cmd_report(args) -> int:
    s = tracking.summary(days=args.days)

    def human(o):
        w = f" (last {o['window_days']} days)" if o["window_days"] else ""
        ev, dec, tr = o["events"], o["decisions"], o["track_record"]
        print(f"AGENT ACTIVITY{w}")
        print(f"  events: {ev['total']}"
              + (f" — " + ", ".join(f"{k}×{n}" for k, n in sorted(ev["by_kind"].items()))
                 if ev["by_kind"] else ""))
        print(f"  decisions: {dec['total']} total, {dec['pending']} pending grade, "
              f"{dec['graded']} graded")
        print("TRACK RECORD (all time)")
        if tr["graded"]:
            print(f"  directional hit rate: {tr['hit_rate']}% over {tr['graded']} graded "
                  f"({tr['avg_fwd_return_pct']:+.2f}% avg forward return)")
            for a, b in tr["by_action"].items():
                if b["graded"]:
                    print(f"    {a:<9} {b['hit_rate']}% of {b['graded']} "
                          f"({b['avg_fwd_return_pct']:+.2f}% avg)")
            if tr["best"]:
                print(f"  best: {tr['best']['ticker']} {tr['best']['action']} "
                      f"{tr['best']['fwd_return_pct']:+.2f}%   "
                      f"worst: {tr['worst']['ticker']} {tr['worst']['action']} "
                      f"{tr['worst']['fwd_return_pct']:+.2f}%")
        else:
            print("  nothing graded yet — record decisions, then run `evaluate` "
                  "after the horizon passes")
        if o["recent_decisions"]:
            print("RECENT DECISIONS")
            for d in o["recent_decisions"]:
                res = ("pending" if d["hit"] is None and d["fwd_return_pct"] is None
                       else f"{d['fwd_return_pct']:+.2f}%"
                       + ("" if d["hit"] is None else " ✓" if d["hit"] else " ✗"))
                print(f"  #{d['id']} {d['ts'][:16]} {d['ticker']:<6} {d['action']:<8} "
                      f"score {d['score'] if d['score'] is not None else '—'}  [{res}]")

    _out(s, args.json, human)
    return 0


def cmd_events(args) -> int:
    rows = tracking.list_events(ticker=args.ticker, kind=args.kind, limit=args.limit)

    def human(rs):
        for r in rs:
            note = (r["note"] or "")[:70]
            print(f"#{r['id']:<5} {r['ts'][:16]} {r['kind']:<10} {(r['ticker'] or ''):<6} "
                  f"{r['source']:<14} {note}")
        print(f"({len(rs)} event(s))")

    _out(rows, args.json, human)
    return 0


def cmd_decisions(args) -> int:
    rows = tracking.list_decisions(ticker=args.ticker, action=args.action,
                                   pending_only=args.pending, limit=args.limit)

    def human(rs):
        for d in rs:
            res = ("pending" if d["evaluated_at"] is None
                   else f"{d['fwd_return_pct']:+.2f}%"
                   + ("" if d["hit"] is None else " ✓" if d["hit"] else " ✗"))
            print(f"#{d['id']:<5} {d['ts'][:16]} {d['ticker']:<6} {d['action']:<8} "
                  f"@ {d['price'] if d['price'] is not None else '—':<9} "
                  f"score {d['score'] if d['score'] is not None else '—':<7} [{res}]")
        print(f"({len(rs)} decision(s))")

    _out(rows, args.json, human)
    return 0


def cmd_news_scan(args) -> int:
    """One Analyst cycle: scan feeds, log the analysis, mail the Trader."""
    from . import newsagent
    res = newsagent.run_scan(send=not args.no_send)

    def human(o):
        a = o["analysis"]
        ov = a["overall"]
        print(f"Scanned {a['n_headlines']} headlines — {ov['sentiment']} tone "
              f"({ov['positive']} pos / {ov['negative']} neg, net {ov['net_ratio']:+.2f})")
        for m in a["macro"]:
            print(f"  macro: {m['topic']} ×{m['count']}")
        for t, d in list(a["tickers"].items())[:8]:
            print(f"  {t:<6} {d['mentions']:>2} mention(s)  net {d['net']:+d}")
        if o["delivered"]:
            print("Delivered to trader:")
            for m in o["delivered"]:
                flag = "❗" if m["priority"] == "high" else "·"
                print(f"  {flag} {m['subject']}")
        if o["deduped"]:
            print(f"({o['deduped']} message(s) suppressed as recent duplicates)")

    _out(res, args.json, human)
    return 0


def cmd_inbox(args) -> int:
    msgs = tracking.inbox(args.agent, unread_only=args.unread, limit=args.limit)

    def human(ms):
        if not ms:
            print(f"Inbox for '{args.agent}' is empty"
                  + (" (no unread)" if args.unread else "") + ".")
            return
        for m in ms:
            flag = "❗" if m["priority"] == "high" else " "
            state = "unread" if m["read_at"] is None else "read"
            print(f"#{m['id']:<5}{flag}{m['ts'][:16]} [{m['kind']}] "
                  f"from {m['from_agent']}"
                  + (f" · {m['ticker']}" if m["ticker"] else "") + f" ({state})")
            print(f"      {m['subject']}")
            if m["body"]:
                print(f"      {m['body'][:300]}")
        print(f"({len(ms)} message(s), {tracking.unread_count(args.agent)} unread total)")

    _out(msgs, args.json, human)
    if args.mark_read and msgs:
        n = tracking.mark_read(args.agent, [m["id"] for m in msgs])
        if not args.json:
            print(f"Marked {n} message(s) read.")
    return 0


def cmd_send(args) -> int:
    m = tracking.send_message(
        args.from_agent, args.to, args.subject, body=args.body, kind=args.kind,
        priority=args.priority, ticker=args.ticker,
        payload=_payload_from(args.payload))
    _out(m, args.json, lambda o: print(
        f"Sent message #{o['id']} {args.from_agent} → {args.to}: {o['subject']}"))
    return 0


def cmd_autonomy(args) -> int:
    """Inspect or change the unattended-trading policy, or gate one order."""
    if args.action == "status":
        p = autonomy.load_policy()
        used = autonomy.trades_today()

        def human(_):
            state = "ENABLED" if p["enabled"] else "DISABLED (kill switch off)"
            print(f"Autonomous trading: {state}")
            print(f"  per-trade cap:      ${p['per_trade_max_usd']:,.2f}")
            print(f"  daily trade cap:    {p['max_trades_per_day']}  (used today: {used})")
            print(f"  defined-risk only:  {p['defined_risk_only']}")
            print(f"  require max loss:   {p['require_max_loss']}")
            print(f"  ticker allowlist:   {p['allowed_tickers'] or 'none (any ticker)'}")
            print(f"  strategy allowlist: "
                  f"{', '.join(p['allowed_strategies']) or 'none (any strategy)'}")
            print(f"  policy file:        {autonomy.POLICY_PATH}")

        _out({**p, "trades_today": used}, args.json, human)
        return 0

    if args.action in ("enable", "disable"):
        p = autonomy.save_policy({"enabled": args.action == "enable"})
        if args.action == "enable":
            print("⚠ Autonomous trading ENABLED. Unattended runs may now place real "
                  "orders without a human approving each one, bounded by:")
            print(f"    ${p['per_trade_max_usd']:,.2f} per trade, "
                  f"{p['max_trades_per_day']} trades/day, "
                  f"defined-risk only: {p['defined_risk_only']}")
            print("  Disable any time with: python -m app autonomy disable")
        else:
            print("Autonomous trading DISABLED. Unattended runs may analyze and log, "
                  "but not place orders.")
        return 0

    if args.action == "set":
        if not args.key:
            print("error: set requires --key and --value", file=sys.stderr)
            return 2
        try:
            p = autonomy.save_policy({args.key: args.value})
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"{args.key} = {p[args.key]}")
        return 0

    if args.action == "check":
        order = _payload_from(args.payload)
        if not isinstance(order, dict):
            print("error: check requires --payload with a JSON object describing the order",
                  file=sys.stderr)
            return 2
        verdict = autonomy.check_order(order)

        def human(v):
            print("ALLOWED" if v["allowed"] else "DENIED")
            for r in v["reasons"]:
                print(f"  ✖ {r}")
            for w in v["warnings"]:
                print(f"  ⚠ {w}")
            print(f"  worst case: {v['worst_case_usd']}  risk: {v['risk_kind']}  "
                  f"trades today: {v['trades_today']}")

        _out(verdict, args.json, human)
        return 0 if verdict["allowed"] else 1

    print(f"error: unknown autonomy action {args.action!r}", file=sys.stderr)
    return 2


def cmd_position_scan(args) -> int:
    """One Position Manager cycle: scan open positions, log, mail the Trader."""
    from . import positionagent
    res = positionagent.run_scan(user_id=args.user_id, send=not args.no_send)

    def human(o):
        a = o["analysis"]
        print(f"Scanned {a['n_positions']} open position(s), "
              f"${a['theta_sum']:.0f}/day portfolio theta")
        print(f"Identified {len(a['actions'])} action(s):")
        for act in a["actions"]:
            print(f"  {act['ticker']:<6} {act.get('type', 'monitor')}: "
                  + "; ".join(act.get("alerts", ["no details"])))
        if o["delivered"]:
            print("Delivered to trader:")
            for m in o["delivered"]:
                flag = "❗" if m["priority"] == "high" else "·"
                print(f"  {flag} {m['subject']}")
        if o["deduped"]:
            print(f"({o['deduped']} message(s) suppressed as recent duplicates)")

    _out(res, args.json, human)
    return 0


def cmd_risk_scan(args) -> int:
    """One Risk Manager cycle: analyze portfolio Greeks, log, mail the Trader."""
    from . import riskagent
    res = riskagent.run_scan(send=not args.no_send)

    def human(o):
        a = o["analysis"]
        print(f"Portfolio: Δ{a['net_delta']:+.0f}, Θ${a['net_theta']:+.0f}/day, "
              f"{a['n_positions']} position(s), {a['risk_level'].upper()} risk")
        if a["issues"]:
            print(f"Identified {len(a['issues'])} risk issue(s):")
            for issue in a["issues"]:
                print(f"  {issue['type'].replace('_', ' ').title()}: {issue['note'][:80]}")
        if o["delivered"]:
            print("Delivered to trader:")
            for m in o["delivered"]:
                flag = "❗" if m["priority"] == "high" else "·"
                print(f"  {flag} {m['subject']}")
        if o["deduped"]:
            print(f"({o['deduped']} message(s) suppressed as recent duplicates)")

    _out(res, args.json, human)
    return 0


def cmd_pattern_scan(args) -> int:
    """One Pattern Engine cycle: analyze decision history, log, mail the Trader."""
    from . import patternagent
    res = patternagent.run_scan(send=not args.no_send)

    def human(o):
        a = o["analysis"]
        if a.get("status") != "ok":
            print(f"Pattern analysis: {a.get('status')} "
                  f"({a.get('n_decisions', 0)}/{a['min_required']} required decisions)")
            return
        findings = a.get("findings", [])
        print(f"Analyzed {a['n_decisions']} graded decision(s), "
              f"found {len(findings)} high-confidence pattern(s)")
        for f in findings[:8]:  # show top 8
            print(f"  {f['category'].replace('_', ' ').title()}: {f['bucket']} "
                  f"({f['win_rate']*100:.0f}% win rate, n={f['sample_size']})")
        if o["delivered"]:
            print("Delivered to trader:")
            for m in o["delivered"]:
                flag = "❗" if m["priority"] == "high" else "·"
                print(f"  {flag} {m['subject']}")
        if o["deduped"]:
            print(f"({o['deduped']} message(s) suppressed as recent duplicates)")

    _out(res, args.json, human)
    return 0


def cmd_export(args) -> int:
    dump = json.dumps(tracking.export_all(), indent=2, default=str)
    if args.out:
        Path(args.out).write_text(dump)
        print(f"Exported tracking data to {args.out}")
    else:
        print(dump)
    return 0


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app",
        description="Trading agent console: signals in, decisions recorded, outcomes graded.")
    p.add_argument("--db", help="override the SQLite path (default: journal.db in repo root)")
    p.add_argument("--policy-file", help="override the autonomy policy path (default: autonomy.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def jflag(sp):
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    sp = sub.add_parser("analyze", help="signal brief for a ticker; records a decision")
    sp.add_argument("ticker")
    sp.add_argument("--period", default="6mo")
    sp.add_argument("--horizon", type=int, default=10, help="grading horizon in trading bars")
    sp.add_argument("--options", action="store_true", help="include options chain read")
    sp.add_argument("--weekly", action="store_true", help="include weekly-timeframe confluence")
    sp.add_argument("--no-record", action="store_true", help="don't record a decision")
    jflag(sp)
    sp.set_defaults(fn=cmd_analyze)

    sp = sub.add_parser("decide", help="record a decision/recommendation explicitly")
    sp.add_argument("--ticker", required=True)
    sp.add_argument("--action", required=True, choices=list(tracking.ACTIONS))
    sp.add_argument("--horizon", type=int, default=10)
    sp.add_argument("--price", type=float)
    sp.add_argument("--score", type=float)
    sp.add_argument("--label")
    sp.add_argument("--rationale")
    sp.add_argument("--order-id")
    sp.add_argument("--source", default="agent-cli")
    sp.add_argument("--snapshot", action="store_true",
                    help="fetch a live signal snapshot to attach (needs network)")
    jflag(sp)
    sp.set_defaults(fn=cmd_decide)

    sp = sub.add_parser("log", help="log one agent/broker event")
    sp.add_argument("kind", help="e.g. order, fill, cancel, data, note")
    sp.add_argument("--ticker")
    sp.add_argument("--note")
    sp.add_argument("--source", default="agent-cli")
    sp.add_argument("--payload", help="inline JSON, @file, or '-' for stdin")
    jflag(sp)
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("ingest", help="store a broker/MCP payload (orders, positions, fills)")
    sp.add_argument("--payload", help="inline JSON, @file, or '-' for stdin")
    sp.add_argument("--source", default="robinhood-mcp")
    sp.add_argument("--kind", help="hint when the shape is ambiguous: orders|positions|fills")
    jflag(sp)
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("scan", help="rank the watchlist's best setups right now (needs network)")
    sp.add_argument("--tickers", help="comma-separated; default is the app's watchlist")
    jflag(sp)
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("evaluate", help="grade matured decisions against real prices")
    sp.add_argument("--period", default="1y", help="history window used for grading")
    jflag(sp)
    sp.set_defaults(fn=cmd_evaluate)

    sp = sub.add_parser("report", help="activity summary + the agent's track record")
    sp.add_argument("--days", type=int, help="restrict the activity window")
    jflag(sp)
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("events", help="list logged events")
    sp.add_argument("--ticker")
    sp.add_argument("--kind")
    sp.add_argument("--limit", type=int, default=50)
    jflag(sp)
    sp.set_defaults(fn=cmd_events)

    sp = sub.add_parser("decisions", help="list recorded decisions")
    sp.add_argument("--ticker")
    sp.add_argument("--action")
    sp.add_argument("--pending", action="store_true", help="only ungraded decisions")
    sp.add_argument("--limit", type=int, default=50)
    jflag(sp)
    sp.set_defaults(fn=cmd_decisions)

    sp = sub.add_parser("news-scan", help="run one Analyst cycle: feeds → analysis → trader inbox")
    sp.add_argument("--no-send", action="store_true", help="analyze and log only, deliver no mail")
    jflag(sp)
    sp.set_defaults(fn=cmd_news_scan)

    sp = sub.add_parser("position-scan", help="run one Position Manager cycle: positions → analysis → trader inbox")
    sp.add_argument("--user-id", type=int, help="scan one user; default: all users")
    sp.add_argument("--no-send", action="store_true", help="analyze and log only, deliver no mail")
    jflag(sp)
    sp.set_defaults(fn=cmd_position_scan)

    sp = sub.add_parser("risk-scan", help="run one Risk Manager cycle: portfolio Greeks → analysis → trader inbox")
    sp.add_argument("--no-send", action="store_true", help="analyze and log only, deliver no mail")
    jflag(sp)
    sp.set_defaults(fn=cmd_risk_scan)

    sp = sub.add_parser("pattern-scan", help="run one Pattern Engine cycle: history → patterns → trader inbox")
    sp.add_argument("--no-send", action="store_true", help="analyze and log only, deliver no mail")
    jflag(sp)
    sp.set_defaults(fn=cmd_pattern_scan)

    sp = sub.add_parser("inbox", help="read an agent's messages")
    sp.add_argument("--agent", default="trader", help="whose inbox (default: trader)")
    sp.add_argument("--unread", action="store_true", help="only unread")
    sp.add_argument("--mark-read", action="store_true", help="mark listed messages read")
    sp.add_argument("--limit", type=int, default=25)
    jflag(sp)
    sp.set_defaults(fn=cmd_inbox)

    sp = sub.add_parser("send", help="send a message between agents")
    sp.add_argument("--from", dest="from_agent", required=True, help="sender agent id")
    sp.add_argument("--to", required=True, help="recipient agent id")
    sp.add_argument("--subject", required=True)
    sp.add_argument("--body")
    sp.add_argument("--kind", default="note", choices=["briefing", "alert", "note"])
    sp.add_argument("--priority", default="normal", choices=["normal", "high"])
    sp.add_argument("--ticker")
    sp.add_argument("--payload", help="inline JSON, @file, or '-'")
    jflag(sp)
    sp.set_defaults(fn=cmd_send)

    sp = sub.add_parser("autonomy", help="unattended-trading policy: status/enable/disable/set/check")
    sp.add_argument("action", choices=["status", "enable", "disable", "set", "check"])
    sp.add_argument("--key", help="policy key for `set`")
    sp.add_argument("--value", help="new value for `set`")
    sp.add_argument("--payload", help="order JSON for `check` (inline, @file, or '-')")
    jflag(sp)
    sp.set_defaults(fn=cmd_autonomy)

    sp = sub.add_parser("export", help="dump all tracking data as JSON (backup)")
    sp.add_argument("--out", help="write to a file instead of stdout")
    sp.set_defaults(fn=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db:
        tracking.DB_PATH = Path(args.db).expanduser()
    if getattr(args, "policy_file", None):
        autonomy.POLICY_PATH = Path(args.policy_file).expanduser()
    tracking.init()
    try:
        return args.fn(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
