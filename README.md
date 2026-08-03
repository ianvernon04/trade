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

Requires Python 3.10+ and internet access (data comes from Yahoo Finance and
public RSS feeds — no API keys needed).

## What's inside

| Tab | What it does |
|---|---|
| **Dashboard** | Live watchlist (big tech + SPY/QQQ) with price, change, and a composite signal per ticker; click a row for a full **agent brief** combining signal + options read + backtest stats + headlines + a concrete trading plan. |
| **Analyze** | Price chart with SMA20/50 + Bollinger bands, RSI, and the composite score history; a table explaining every indicator's vote and weight. |
| **Options** | Full chain for any expiry with computed Black-Scholes greeks (Δ/Θ), put/call ratios, ATM IV, expected move, IV skew, max pain, unusual-activity scanner, and a **CALL / PUT / NO TRADE** recommendation with a suggested ~0.35-delta contract. |
| **Backtest** | Tests the exact signal shown live over 1-5 years with configurable thresholds, ATR stop/target, and holding period. Reports win rate, profit factor, expectancy, drawdown, equity curve, per-trade log, and an option-proxy return. |
| **Tech News** | Live headlines merged from CNBC, TechCrunch, The Verge, Ars Technica, Wired, MarketWatch, Yahoo Finance, Investing.com, Seeking Alpha, and Engadget, deduped, sentiment-tagged, filterable, auto-refreshing every 3 min. |
| **Journal** | Log entries/exits (options or stock). P&L is computed automatically (×100 per options contract), with win rate, profit factor, avg win/loss, per-ticker P&L and a cumulative P&L curve. Stored locally in `journal.db` (SQLite). |
| **Learn** | Options 101 for beginners: what calls/puts are, the greeks, IV & IV crush, starter strategies, risk rules, common mistakes — plus curated free videos and courses (OIC, Cboe, Investopedia, YouTube topics) with a suggested learning path. |

The app also tracks **earnings dates**: the Options tab and Agent brief show the
next report date and raise a ⚠ IV-crush warning whenever earnings land before
the option expiry you're looking at (or within 7 days), and the trading plan
tells you to be flat before the report unless the earnings bet is the thesis.

Quotes/signals refresh every 30 s while a tab is open; option chains are cached
60 s server-side, news 3 min.

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

**Before you share, know these two caveats:**

- **One shared journal.** The app has a single trade journal with no user
  accounts — anyone with access sees and can edit the same trades. Set the
  `APP_PASSWORD` environment variable to require a password (any username,
  that password) so strangers can't reach your data; for a group of friends,
  better to have each person run their own copy.
- **Hosted journals can reset.** On free hosts the disk is ephemeral —
  `journal.db` is wiped on redeploys/restarts. Keep the journal on a computer
  you own if the history matters (it does).

## Data sources

- **Market data & option chains:** Yahoo Finance (via `yfinance`) — quotes,
  OHLCV history, chains with IV and open interest.
- **News:** public RSS feeds from the outlets listed above.
- Greeks are computed locally with Black-Scholes (risk-free ≈ 4.5%).

Yahoo quotes can be ~15 min delayed for some exchanges; treat "live" as
near-real-time, and always confirm fills/prices in your broker.
