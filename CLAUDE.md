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
  call any Robinhood order-placing tool.** A standing "trade for me" from
  earlier does not count as approval for a new, specific order. The *only*
  exception is an unattended autonomous run, which is governed by the
  separate, narrower protocol below — if a human is present in the
  conversation, this rule applies with no exceptions.
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

## Autonomous (unattended) trading

An unattended run is any session with no human reading along in real time —
a scheduled Routine, a cron-fired session, a background job. The rules above
assume a human is present to hear a stated risk and answer; here nobody is,
so the check moves into code (`app/autonomy.py`) and this protocol is
mandatory.

**Before every unattended order, without exception:**

```
python -m app autonomy check --payload '{"ticker":"NVDA","strategy":"long_call",
  "side":"buy","quantity":2,"strike":185,"expiry":"2026-09-18",
  "limit_price":1.20,"est_cost":240,"max_loss":240}'
```

Exit code 0 = allowed, 1 = denied. **If it denies, do not place the order and
do not work around it** — log the denial (`python -m app log note`) and move
on. Never edit the policy mid-run to make a denied order pass; the policy is
the account owner's standing instruction, not a suggestion to negotiate with.

**The moment the broker tool returns**, record it so it counts toward the
daily cap and lands in the audit trail:

```python
from app import autonomy
autonomy.record_autonomous_order(order, payload=<tool result JSON>)
```

The gate enforces (all configurable, `python -m app autonomy status`):

- **Kill switch** — off by default; nothing autonomous trades until the owner
  runs `python -m app autonomy enable`.
- **Per-trade cap** ($300 default) measured on max loss, not cost, so a
  credit spread is judged by what it can actually lose.
- **Daily trade cap** (2 default) counting only `source='autonomous'` orders,
  so human-approved trades never consume the unattended budget.
- **Defined-risk only** (on by default) — unattended runs may not open naked
  or unrecognized-strategy positions. An unknown strategy is treated as
  undefined risk, never as safe.
- **Known worst case required** — no order whose max loss can't be stated as
  a number.

Also true of unattended runs: the tracking discipline below applies in full,
and the earnings/IV/macro warnings must still be evaluated — with nobody to
read them, a warning that would make a human hesitate is a reason to skip the
trade, not to note it and proceed.

**Honest limitation:** this gate cannot physically intercept an MCP call —
the broker tools are directly reachable. It is a mandatory protocol step with
a deterministic, tested implementation and an audit trail, not a sandbox. The
kill switch (`python -m app autonomy disable`) and Robinhood's own account
controls are the hard stops.

## CLI reference

```
python -m app analyze NVDA [--options] [--weekly] [--horizon 10] [--json]
python -m app decide --ticker NVDA --action call --price 181.2 --rationale "..." [--snapshot]
python -m app log proposal --ticker NVDA --note "<exact order ticket stated in chat>"
python -m app log order --ticker NVDA --source robinhood-mcp --payload '{...}' | @file | -
python -m app ingest --payload - [--kind positions] [--source robinhood-mcp]
python -m app evaluate [--period 1y]        # grade matured decisions
python -m app report [--days 30]            # activity + track record
python -m app events / decisions [--pending] [--json]
python -m app export --out backup.json      # journal.db is gitignored — this is the backup
python -m app autonomy status               # unattended-trading policy + today's usage
python -m app autonomy enable | disable     # the kill switch
python -m app autonomy set --key per_trade_max_usd --value 300
python -m app autonomy check --payload '{...}'   # exit 0 = allowed, 1 = denied
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
