# Options Trading Assistant

A self-hosted, auto-refreshing web app that acts as a trading co-pilot for
options (and stocks): live market data, technical buy/sell signals, options
chain analytics with call/put recommendations, a backtesting engine, live
technology news from a dozen sources, and a trade journal with P&L analytics.

> **Disclaimer:** this is an educational/analytical tool, **not financial
> advice**. Options can lose 100% of the premium paid. No signal or backtest
> guarantees future results.

## Quick start

**Windows:** double-click `start.bat` — it installs dependencies, starts the
server, and opens your browser automatically.

**macOS:** double-click `start.command` (first time: right-click → Open, since
it's unsigned). Same behavior.

**Linux / terminal:**

```bash
./run.sh              # installs deps and starts the server
# then open http://127.0.0.1:8000
```

or manually:

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

**Run it in the background (macOS)** — no terminal window to keep open:

```bash
./install-server.sh            # starts now, auto-starts at login, restarts on crash
./install-server.sh --status   # is it running?
./install-server.sh --logs     # watch the server log
./install-server.sh --remove   # stop and uninstall
```

The dashboard is then always at http://127.0.0.1:8000 while the Mac is awake
and you're logged in. (For a URL that works even when this computer is off —
including from your phone — use the Render deployment below; note its free
tier wipes `journal.db` on redeploys.)

Requires Python 3.10+ and internet access (data comes from Yahoo Finance and
public RSS feeds — no API keys needed).

## What's inside

| Tab | What it does |
|---|---|
| **Dashboard** | Live watchlist (big tech + SPY/QQQ) with price, change, and a composite signal per ticker; click a row for a full **agent brief** combining signal + options read + backtest stats + scorecard + weekly-timeframe confluence + headlines + a concrete trading plan. |
| **Neighborhood** | A drawn street where your agents live. **The Trader (№1)**: windows lit when awake, porch light follows its stance (green/red/blue), chimney smoke when active, mailbox flag up with its unread-mail count, career hit rate on the lawn plaque — click to step inside (Agent tab). **The Analyst (№2)**: rooftop antenna, a robot reading the paper in the window, porch light showing the market's news tone, mailbox counting briefings sent to the Trader today — click for headlines (Tech News tab). Then three cottages for the junior agents, each with its own rooftop mark, a porch light coloured by what it last found, a mailbox counting today's briefings, and one live stat on the plaque: **Positions (№3)** — a watchtower cupola, "6 positions · 2 to review"; **Risk (№4)** — scales on the ridge, "Δ-1240 · high risk"; **Patterns (№5)** — a memory spire, "3 edges from 64 calls". Any house goes dark and starts snoring when its agent hasn't run recently. The sky follows your local clock — sun by day; moon, stars, and lit streetlamps at night (`?hour=12` / `?hour=22` to preview). |
| **Agent** | The Claude agent's visible body: an avatar whose mood follows its latest stance (bullish / bearish / watching / asleep), a speech bubble with its current thinking, a live diary of every analysis, order, and fill it performs through the Robinhood MCP server, and its graded report card — hit rate, average forward return, best call — from the tracking store. |
| **Scanner** | Morning sweep of the watchlist ranked by signal strength, fresh threshold crossings, and daily/weekly agreement (earnings drag rank down). Includes a macro calendar (FOMC/CPI) and a live **alert feed**: a background engine re-checks the watchlist and your open positions every 5 minutes and raises alerts for signal crossings, positions near expiry/earnings, and expired contracts — with optional browser/phone notifications. |
| **Analyze** | Price chart with SMA20/50 + Bollinger bands, RSI, and the composite score history; a table explaining every indicator's vote and weight; and the **agent scorecard** — every historical crossing of the live signal on this ticker over 3 years, graded on forward returns, so you know the signal's actual win rate before trusting it. |
| **Options** | Full chain for any expiry with computed Black-Scholes greeks (Δ/Θ), put/call ratios, ATM IV, **IV context** (current IV vs the past year's realized-vol distribution), expected move, IV skew, max pain, unusual-activity scanner, and a **CALL / PUT / NO TRADE** recommendation with a suggested ~0.35-delta contract **plus a defined-risk vertical spread alternative** (strikes, max loss/profit, breakeven). |
| **Backtest** | Tests the exact signal shown live over 1-5 years with configurable thresholds, ATR stop/target, and holding period. Reports win rate, profit factor, expectancy, drawdown, equity curve, per-trade log, and an option-proxy return. A **"Find best settings" optimizer** grid-searches 36 configurations (with an explicit overfitting warning). |
| **Tech News** | Live headlines merged from CNBC, TechCrunch, The Verge, Ars Technica, Wired, MarketWatch, Yahoo Finance, Investing.com, Seeking Alpha, and Engadget, deduped, sentiment-tagged, filterable, auto-refreshing every 3 min. |
| **Journal** | Per-user trade journals: create an account (username + password) and log entries/exits (options or stock). P&L is computed automatically (×100 per options contract), with win rate, profit factor, avg win/loss, per-ticker P&L and a cumulative P&L curve — each user sees only their own trades. Includes **live open-position tracking** (marks from live option mids, per-position and portfolio delta/theta, expiry/earnings/drawdown flags) and a **journal coach** that finds patterns in your own trades (by type, holding period, ticker). Stored locally in `journal.db` (SQLite, passwords PBKDF2-hashed). |
| **Learn** | Options 101 for beginners: what calls/puts are, the greeks, IV & IV crush, starter strategies, risk rules, common mistakes — plus curated free videos and courses (OIC, Cboe, Investopedia, YouTube topics) with a suggested learning path. |

The app also tracks **earnings dates**: the Options tab and Agent brief show the
next report date and raise a ⚠ IV-crush warning whenever earnings land before
the option expiry you're looking at (or within 7 days), and the trading plan
tells you to be flat before the report unless the earnings bet is the thesis.

Quotes/signals refresh every 30 s while a tab is open; option chains are cached
60 s server-side, news 3 min.

## Claude + Robinhood agent mode

The repo doubles as a Claude Code agent workspace wired to Robinhood:

- **`.mcp.json`** registers the `robinhood-trading` MCP server
  (`https://agent.robinhood.com/mcp/trading`). Open the repo in Claude Code,
  approve the server when prompted, and authenticate with `/mcp` — Claude can
  then use Robinhood's account/market/order tools alongside this app's
  signals. (Same effect as
  `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`,
  but project-scoped so it travels with the repo.)
- **`CLAUDE.md`** tells every Claude session how to behave: analyze with the
  app's indicators before recommending, log every broker action, never place
  an order without your explicit confirmation. Its **real-money safety
  rules** encode the actual risk parameters you set — confirmation ceremony
  by order size, position-size limits (or lack thereof), and whether
  undefined-risk (naked) options are allowed — so every session follows the
  same rules instead of improvising them. Edit that section directly to
  change your limits.
- **Agent tracking** (`app/tracking.py`) records what the agents do in
  `journal.db`: `agent_events` (orders, fills, positions, data pulls — raw
  payloads preserved) and `agent_decisions` (every recommendation with its
  full signal snapshot). `python3 -m app evaluate` later grades each
  directional call against the realized forward move, so
  `python3 -m app report` shows the agent's actual hit rate — the same
  self-accountability the scorecard applies to the signal.
- **The Analyst** (`app/newsagent.py`) is agent №2: every 15 minutes while
  the server runs (or via `python3 -m app news-scan`), it sweeps all the news
  feeds, aggregates sentiment per ticker, and watches macro-risk topics
  (Fed/rates, CPI, tariffs, geopolitics, credit). Findings land in the
  Trader's **inbox** (`agent_messages` — the inter-agent message bus):
  routine digests as briefings, headline clusters and macro topics as
  high-priority alerts, deduped so a 15-minute cadence can't flood the box.
  The Trader reads its mail at the start of every session
  (`python3 -m app inbox --agent trader --unread`), and autonomous runs
  treat a fresh high-priority alert as a reason to sit out. The Analyst
  never touches broker tools: it informs, the Trader decides.
- **Three more agents** ride the same bus, on the same terms — analyze, mail
  the Trader, never call a broker tool. **The Position Manager**
  (`app/positionagent.py`, every 30 min or `python3 -m app position-scan`)
  watches open positions for exits and rolls: profit-taking, broken theses,
  expiry inside a week, earnings landing before expiration. **The Risk
  Manager** (`app/riskagent.py`, hourly or `python3 -m app risk-scan`) works
  at the portfolio level instead of the position level — net delta and theta,
  single-ticker concentration, gross leverage, unhedged exposure. **The
  Pattern Engine** (`app/patternagent.py`, every 4 hours or
  `python3 -m app pattern-scan`) mines the graded decision history for
  repeatable edge — which tickers, directions, IV regimes, and signal
  strengths actually win — and reports once it has 20+ graded calls to
  reason from.

The console works standalone too:

```bash
python3 -m app analyze NVDA --options   # signal brief, records the decision
python3 -m app report                   # activity + graded track record
python3 -m app export --out backup.json # journal.db is gitignored; back it up
```

Everything is also exposed under `/api/tracking/*` when the server runs.
Trading through the agent is still your decision at every step: it is built
to require explicit confirmation per order, and its analytics remain
educational, not financial advice.

## How the signal works

Seven weighted components vote between -1 and +1; the total is normalized to a
score in [-100, +100]:

- Trend (25): price vs 50/200-day MAs and the 20/50 stack
- MACD (20): sign + histogram expansion
- RSI (15): oversold/overbought mean-reversion
- Stochastic (10): %K/%D crosses at extremes
- Bollinger (10): position within the bands
- Momentum (10): 10-bar rate of change
- Volume (10): OBV vs its 20-bar average

Score ≥ +40 → STRONG BUY, ≥ +15 → BUY, ≤ -15 → SELL, ≤ -40 → STRONG SELL.
The options recommendation then adjusts this with put/call flow and IV skew,
and suggests a ~0.35-delta contract 30-45 DTE when there's an edge.

The backtester runs the *same* score series historically, so what you test is
what you trade.

## API

Everything the UI uses is a plain JSON endpoint you can script against:

```
GET  /api/watchlist?tickers=AAPL,NVDA     quotes + signals
GET  /api/quote/{ticker}                  live quote
GET  /api/analysis/{ticker}?period=6mo    indicators + signal + chart data
GET  /api/options/{ticker}?expiry=YYYY-MM-DD
GET  /api/backtest/{ticker}?period=2y&buy_threshold=20...
GET  /api/agent/{ticker}                  full combined brief
GET  /api/news                            merged tech headlines
GET  /api/news/{ticker}                   per-ticker headlines
GET/POST/PATCH/DELETE /api/journal        trade CRUD
GET  /api/journal/stats                   P&L analytics
```

## Phone / no-download use (PWA)

The app is a Progressive Web App: once it's hosted at a URL (see below),
visitors need no download — and on a phone they can install it like a native
app: **iPhone Safari** → Share → *Add to Home Screen*; **Android Chrome** →
⋮ menu → *Add to Home screen / Install app*. It then launches full-screen
from its own ⚡ icon. The service worker caches the app shell for instant
loads; market data always comes from the network.

## Sharing it with others

Three ways, from easiest to most public:

**1. Same Wi-Fi / home network.** Start with:

```bash
HOST=0.0.0.0 ./run.sh        # Mac/Linux
```

(Windows: `set HOST=0.0.0.0` then run `start.bat`.) Find your computer's local
IP (`ipconfig` on Windows, `ifconfig`/`ip a` on Mac/Linux — something like
`192.168.1.23`), and anyone on your network can open `http://192.168.1.23:8000`.

**2. Free cloud hosting (public URL).** The repo ships with a `render.yaml`
blueprint and a `Dockerfile`:

- [Render](https://render.com): New + → **Blueprint** → select this repo →
  deploy. You get a `https://….onrender.com` URL anyone can open. Free tier
  sleeps after idle periods (first hit takes ~30 s to wake).
- Any Docker host (Railway, Fly.io, a VPS): `docker build -t trade . && docker
  run -p 8000:8000 trade`.

**3. Sharing the code.** Make the GitHub repo public (repo → Settings →
General → Danger Zone → Change visibility) so anyone can download and run
their own copy — each person then gets their own private journal.

**Before you share, know these caveats:**

- **Journals are per-account.** Each person creates their own username +
  password in the Journal tab and sees only their own trades. (Trades logged
  before accounts existed are adopted by the first account created.) You can
  additionally set the `APP_PASSWORD` environment variable to gate the whole
  site behind one shared password — useful to keep strangers off a public URL.
- **Hosted journals can reset.** On free hosts the disk is ephemeral —
  `journal.db` (accounts and trades) is wiped on redeploys/restarts. Keep the
  journal on a computer you own if the history matters (it does).
- **Use unique passwords.** Accounts are hashed properly (PBKDF2), but this is
  a hobby app served over whatever transport you give it — don't reuse a
  password you care about, and prefer hosts that provide HTTPS (Render does).

## Health checks

`healthcheck.py` exercises every data path against live data and reports
problems — run it any time something looks off:

```bash
python3 healthcheck.py                                          # local app + live data
python3 healthcheck.py --url https://your-app.onrender.com      # also check the deployed site
python3 healthcheck.py --quiet                                  # only print problems
```

It verifies Yahoo connectivity, price history (including that no NaN bars slip
through), the signal engine, option chains and greeks, earnings lookups (and
that ETFs are correctly skipped), the backtester, scorecard, scanner, news
feeds, the macro calendar, and every API endpoint — asserting each response is
strict-JSON clean. Exit code 1 on failure, so CI can gate on it. If the machine
has no internet, data checks are reported as SKIPPED rather than failed.

A GitHub Actions workflow (`.github/workflows/healthcheck.yml`) runs it every
weekday at 11:55 UTC (7:55am ET, before the open) and opens an issue labeled
`healthcheck` if anything fails. To have it also check your deployed site, add
a repository variable named `APP_URL` (Settings → Secrets and variables →
Actions → Variables) set to your app's URL.

## Data sources

- **Market data & option chains:** Yahoo Finance (via `yfinance`) — quotes,
  OHLCV history, chains with IV and open interest.
- **News:** public RSS feeds from the outlets listed above.
- Greeks are computed locally with Black-Scholes (risk-free ≈ 4.5%).

Yahoo quotes can be ~15 min delayed for some exchanges; treat "live" as
near-real-time, and always confirm fills/prices in your broker.
