# New World Logic

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

## Built for Everyday Investors *and* Technical Users (3 Adaptive Modes)

New World Logic features a live **3-Tier Level of Understanding switcher** in the top bar (`🌱 Beginner / Everyday` · `⚡ Trader` · `🔬 Quant`) plus an interactive **`📖 Explain` Deep-Dive Drawer**:

- **🌱 Everyday / Beginner Mode:** Translates complex market data into a plain-English **Guided Coach Card** (*What the signal means*, *Why it's moving*, *How much a contract costs in plain dollars*, and *What could go wrong*), with real-world analogies and step-by-step action checklists.
- **⚡ Trader Mode:** Focuses on actionable swing/options setups — composite technical scores (`-100` to `+100`), `TREND` vs. `RANGE` regime classification, `1.5×ATR` stops and `3.0×ATR` targets (`2.0R`), daily/weekly confluence, and liquid ~35-DTE `0.35Δ` option contracts + vertical spreads.
- **🔬 Quant / Technical Mode:** Exposes the exact math, formulas, and telemetry under the hood — normalized weight vectors, Black-Scholes $d_1, d_2$ and portfolio-level signed Greeks ($\Delta, \Gamma, \Theta, \mathcal{V}$), 6-bucket self-learning Pattern Engine win-rate priors, and theta/slippage drag decomposition in the backtester.

## What's inside

| Tab | What it does |
|---|---|
| **Dashboard** | Live 17-ticker watchlist (mega-cap tech + affordable liquid names `INTC`, `SOFI`, `F`, `RIVN` + `SPY`/`QQQ`/`IWM`) with price, change, `TREND`/`RANGE` regime, and composite signal; click a row for a full **agent brief** + **3-Tier Guided Coach** combining signal, 35-DTE options read, backtest stats, scorecard, weekly confluence, and live headlines. |
| **Neighborhood** | A drawn street where your 5 autonomous agents live: **The Trader (№1)**, **The Analyst (№2)**, **Positions (№3)**, **Risk (№4)**, and **Patterns (№5)**. Each cottage reflects live agent status, unread briefings, and portfolio/pattern metrics. |
| **Agent** | The Claude agent's visible body, live diary of analyses/orders/fills, and graded report card from `agent_decisions`. |
| **Scanner** | Parallelized morning sweep of the watchlist ranked by signal strength, fresh threshold crossings, and daily/weekly agreement (imminent earnings drag rank down). Includes macro calendar (`FOMC`/`CPI`) and 5-minute background alert engine. |
| **Analyze** | Price chart with `SMA 20/50`, Bollinger bands, `RSI-14`, `MACD`, and composite score history; indicator vote table with `ⓘ Explain` buttons; and the 3-year **agent scorecard**. |
| **Options** | Auto-selects the ~35 DTE sweet-spot expiry by default, filters for liquid strikes (tight bid-ask spread & open interest), computes full Black-Scholes Greeks ($\Delta, \Gamma, \Theta, \mathcal{V}$), IV Rank/Percentile, IV skew, max pain, unusual activity, and recommends a `~0.35Δ` contract plus a defined-risk vertical spread. |
| **Backtest** | Tests the live composite signal over 1–5 years (with neutral `sma200` warm-up), reporting stock equity curves plus a realistic **35-DTE Option Proxy** that accounts for daily theta decay (`~1.1%/bar`) and bid-ask slippage (`1.5%`) alongside the naive 5x proxy. |
| **Tech News** | Live headlines merged from CNBC, MarketWatch, Seeking Alpha Market Currents, Nasdaq Markets, Google Finance News, TechCrunch, The Verge, Ars Technica, Wired, and Engadget — with 17-ticker entity recognition, 3-tier per-ticker fallback, and highlighted sentiment trigger words. |
| **Journal** | Multi-user SQLite trade journal (WAL mode enabled) with live open-position marking and full portfolio-level signed Greeks ($\Delta_{\text{shares}}, \Gamma_{\text{shares}}, \Theta_{\$/\text{day}}, \mathcal{V}_{\$}$). |
| **Learn** | Interactive Options 101 & Deep-Dive Explainer library covering signals, Greeks, IV crush, position sizing, and risk rules at Beginner, Trader, and Quant depth. |

The app also tracks **earnings dates**: the Options tab and Agent brief show the
next report date and raise a ⚠ IV-crush warning whenever earnings land before
the option expiry you're looking at (or within 7 days), and the trading plan
tells you to be flat before the report unless the earnings bet is the thesis.

Quotes/signals refresh every 30 s while a tab is open; option chains are cached
60 s server-side, news 3 min. On mobile viewports (`≤768px`), a native-style
5-tab bottom navigation bar and slide-up explainer sheet provide a thumb-friendly
experience.

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
- **Where the position agents get the account** (`app/portfolio.py`): from the
  broker, not the Journal. Whenever a Claude session pulls positions from the
  Robinhood MCP server and records them with `python3 -m app ingest`, that
  snapshot becomes the book those two agents reason over — so they work for
  someone who never opens the Journal tab. They then gather every other input
  themselves: live quotes, option marks, greeks, days to expiry, earnings
  dates. The agents can't call Robinhood directly (they're background threads
  in the web server; MCP tools live in a Claude session), so a session
  refreshing that snapshot is what keeps them current. Crucially they
  distinguish **an empty book from an unseen one**: with no snapshot they
  report *blind* and alert the Trader instead of quietly reporting "no
  positions", and a snapshot over 12 h old is labelled stale. A position whose
  option chain won't load is counted as *unpriced* and named, so a theta or
  delta total never silently omits real exposure.

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
