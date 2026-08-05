# Claude agent guide for this repo

This is a self-hosted options/stock trading assistant (FastAPI + vanilla JS
PWA). You — a Claude session in this repo — act as its trading agent: you
combine the **Robinhood MCP server** (account, live data, order tools) with
the app's **own indicators and predictions**, and you leave a paper trail of
every action and every call you make so the system can grade itself.

## Module map

- `app/main.py` — FastAPI server (`uvicorn app.main:app`), all `/api/*` routes
- `app/data.py` — Yahoo Finance quotes/history/chains (cached, rate-limit tolerant)
- `app/indicators.py`, `app/signals.py` — indicator stack and the weighted
  composite score in [-100, +100] with per-component votes
- `app/options.py` — chain analytics, greeks, IV context, CALL/PUT/NO TRADE rec
- `app/backtest.py`, `app/scorecard.py` — historical validation of the same signal
- `app/journal.py` — the human's manual UI journal (per-user, auth'd)
- `app/tracking.py` — **the agent's memory**: every event, decision, and graded
  outcome (tables `agent_events` / `agent_decisions` in `journal.db`)
- `app/agent_cli.py` — your console: `python -m app <command>`
- `tests/` — `python -m unittest discover -s tests`

## Robinhood MCP server

`.mcp.json` registers `robinhood-trading` (HTTP,
`https://agent.robinhood.com/mcp/trading`). Tools appear as
`mcp__robinhood-trading__*` once the user approves the server and
authenticates (`/mcp` → login). If the tools are missing or unauthenticated,
say so and fall back to Yahoo-backed app modules for data; never fake broker
state.

## The tracking discipline (non-negotiable)

The value of this system compounds only if the dataset is complete. Follow
these rules in every session:

1. **Start of session:** `python -m app evaluate` (grade matured calls), then
   `python -m app report` — know the track record before making new calls.
2. **Before any recommendation or trade:** `python -m app analyze TICKER`
   (add `--options --weekly` for conviction checks). This records the
   decision with its full signal snapshot automatically. Use
   `--no-record` only for idle exploration.
3. **Every state-changing broker action** (order placed / modified /
   cancelled via MCP) must be logged the moment the tool returns:
   `python -m app log order --ticker X --source robinhood-mcp --payload '<tool result JSON>'`
4. **Bulk MCP data** (orders, fills, positions, portfolio) goes through
   `python -m app ingest --payload '-'` (pipe the tool result via stdin) —
   it normalizes recognizable shapes and always preserves the raw payload.
5. **Link executions to reasoning:** when an order fills, record or update the
   decision with `--order-id` so fills trace back to the signal that caused them.
6. **Never invent data.** Log only what tools actually returned. If a call
   failed, that's an event too (`python -m app log note --note "..."`).
7. **Manual trades the user mentions** belong in the tracking store as well
   (`decide` + `log`), so the dataset covers everything, not just agent acts.

## Real-money safety rules

These are the specific risk rules the account owner chose after being asked
directly (2026-08-05) — not generic best practices. Follow them exactly:
don't loosen them because a trade "seems obviously fine," and don't add
unrequested friction either.

- **Every order needs its own explicit "yes" in this conversation before you
  call any Robinhood order-placing tool — no exceptions.** A standing "trade
  for me" from earlier does not count as approval for a new, specific order.
  Ceremony scales with size:
  - **Under $300 total premium/cost:** state the trade in one line (ticker,
    contract, side, qty, approx cost) and get a plain yes.
  - **$300 or more:** lay out a full order ticket first — exact contract
    (ticker, strike, expiry, call/put, side, qty, limit/market), total cost,
    and max loss — then get explicit approval of that ticket.
- **No hard position-size cap is set.** Always state the size and total
  cost/max-loss plainly as part of the confirmation above — the human
  approval *is* the size check here, since nothing caps it automatically.
- **Undefined-risk (naked) options are allowed.** Every time one is
  proposed, regardless of size, state the actual worst case in plain
  language before asking for approval:
  - Naked short call: loss is not capped — it grows as the stock keeps
    rising, with no ceiling.
  - Naked short put: max loss is strike × 100 × contracts if the stock goes
    to zero.
  Get explicit approval of *that specific risk*, not just the trade idea.
- **Options-specific warnings still apply no matter the ceremony tier:**
  earnings-before-expiry (IV crush), elevated IV percentile, and upcoming
  macro events (`/api/calendar`) — surface these before any options trade.
- **Log the proposal, not just the fill.** The moment you state a ticket —
  before calling the order tool — record it:
  `python -m app log proposal --ticker X --note "<the exact ticket stated>"`.
  That's the durable proof of what was actually proposed and approved,
  independent of chat scrollback. Log the resulting order/fill as required
  below once the tool returns, and link them with `--order-id` where possible.
- Signals here are educational analytics, not financial advice; say so when
  the stakes warrant it. Options can lose 100% of premium — naked positions
  can lose more.

## Autonomous mode

The account owner asked for a version of this agent that can trade **without
a live conversation** — e.g. invoked headlessly on a schedule. The rules
above assume a human is present to say "yes" to each order; autonomous mode
has no one there, so it cannot use them as written. This section is what
replaces live approval, and it is deliberately narrower than what's allowed
when the owner is actually chatting with you.

**How you know you're in this mode:** you were started non-interactively
(e.g. `claude -p "..."`) for the express purpose of running the routine
below, with no human watching in real time. If there's any doubt — if this
could be a live conversation — treat it as live mode and follow the rules
above instead; autonomous mode is the exception, not the default.

**The routine:**
1. `python -m app evaluate` then `python -m app report --days 30`. If the
   last 30 days have 5+ graded decisions and a hit rate under 40%, stop —
   log a note explaining why (`python -m app log note --note "..."`) and do
   not place any trades this run. A cold streak is exactly when autonomous
   size should shrink to zero, not when it should keep firing.
2. `python -m app scan` (or `GET /api/scan` if the server's up) to rank
   today's setups.
3. A setup may be **traded automatically only if every one of these hold**:
   - `crossed_buy` or `crossed_sell` is true (a fresh threshold cross, not
     just an already-established score) **and** `confluence` is `"agree"`
     (daily and weekly timeframes agree) — this is deliberately a higher bar
     than the live-chat rules use, since no human is sanity-checking it.
   - `earnings_in_days` is `null` or beyond the option's expiry — skip
     entirely (log a note, no trade) if earnings fall before expiry. IV-crush
     judgment calls need a human; autonomous mode doesn't make them.
   - The structure is **defined-risk only** — a long call/put, or a vertical
     spread. Naked/undefined-risk options are allowed in live chat because
     you can hear the account owner explicitly accept that specific risk;
     with nobody listening, autonomous mode never opens one, full stop.
   - Total premium/cost is **at or under $300** and this would be the
     **only** new autonomous position opened today. (Live chat has no hard
     cap because the owner's live approval *is* the check; here, nothing
     plays that role, so a cap does.)
   Anything that fails any of these gets logged as a decision/note and
   skipped — not executed, not deferred for "next time," just recorded for
   the owner to review later.
4. For a setup that clears all four gates: log the proposal
   (`python -m app log proposal ...`) with the exact contract *before*
   calling the order tool, place it, then log the resulting order/fill and
   link it with `--order-id` — identical to the live-chat discipline above.
5. Whether or not anything traded, end by logging a `note` event summarizing
   the run (what was scanned, what passed/failed the gates, what happened)
   so a human reading the diary later has the full picture without needing
   to have watched it happen.

These specific thresholds (30-day/40%/5-call circuit breaker, $300 cap, one
position/day, defined-risk-only) are defaults chosen because the owner
declined to specify them when asked — not a guess at generic best practice.
They're deliberately conservative *because* nobody's watching. Change them
by editing this section directly.

**What you're on your own to verify before trusting this with real money —**
none of this could be tested from the sandbox that built it:
- **This needs a scheduler on your own machine.** Nothing in this repo
  creates one. On macOS, `crontab -e` and a line like
  `0 9 * * 1-5 cd ~/Desktop/trade && claude -p "Run the autonomous mode routine in CLAUDE.md" >> autolog.txt 2>&1`
  runs it weekdays at 9am — adjust the path and time, and confirm
  `claude --help` still shows `-p`/`--print` on your installed version.
- **Robinhood re-authentication in a headless run is unverified.** `/mcp`
  login opens a real browser; whether that session's credentials survive a
  scheduled, unattended invocation days later is untested. Watch the first
  few scheduled runs in real time (check `autolog.txt` and `python -m app
  report` right after) before trusting one to run unwatched overnight.
- **Robinhood's terms of service for automated order submission through
  their conversational interface haven't been checked.** Confirm this is
  permitted before relying on it.

## CLI reference

```
python -m app analyze NVDA [--options] [--weekly] [--horizon 10] [--json]
python -m app decide --ticker NVDA --action call --price 181.2 --rationale "..." [--snapshot]
python -m app log proposal --ticker NVDA --note "<exact order ticket stated in chat>"
python -m app log order --ticker NVDA --source robinhood-mcp --payload '{...}' | @file | -
python -m app ingest --payload - [--kind positions] [--source robinhood-mcp]
python -m app scan [--tickers AAPL,MSFT,...] [--json]  # rank today's setups (autonomous mode)
python -m app evaluate [--period 1y]        # grade matured decisions
python -m app report [--days 30]            # activity + track record
python -m app events / decisions [--pending] [--json]
python -m app export --out backup.json      # journal.db is gitignored — this is the backup
```

Actions: `buy sell call put hold no_trade`. Decisions are graded on the
`horizon` forward move: directional actions get hit/miss, flat calls just get
the realized move stamped.

When the web server is running, the same store is reachable at
`GET/POST /api/tracking/{summary,events,decisions,event,decision,ingest,evaluate,export}`.

## Dev

- Run: `./run.sh` or `uvicorn app.main:app --port 8000` (deps:
  `pip install -r requirements.txt`, Python 3.10+)
- Tests: `python -m unittest discover -s tests` — keep them green; new
  tracking logic needs tests (stdlib-only, inject prices, no network).
- `journal.db` (journal + tracking tables) is local state, never committed.
- Style: match the existing modules — module docstring explaining *why*,
  `from __future__ import annotations`, guarded degradation over hard failure.
