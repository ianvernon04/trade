/* New World Logic — frontend. Plain JS, no dependencies. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COLORS = { s1: css("--series-1"), s2: css("--series-2"), s3: css("--series-3"),
  good: css("--good"), crit: css("--critical"), muted: css("--muted"),
  grid: css("--grid"), baseline: css("--baseline"), ink: css("--ink") };

let currentTicker = "NVDA";
const timers = {};

/* ---------------- tiny chart library (single y-axis, crosshair tooltip) ---------------- */

const charts = {}; // canvas id -> chart state

function drawChart(canvasId, labels, series, opts = {}) {
  const canvas = $("#" + canvasId);
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  const cssH = parseInt(canvas.getAttribute("height") || 240, 10);
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  canvas.style.height = cssH + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const padL = 52, padR = 10, padT = 10, padB = 20;
  const W = cssW - padL - padR, H = cssH - padT - padB;
  const vals = series.flatMap(s => s.data).filter(v => v !== null && v !== undefined && isFinite(v));
  if (!vals.length) return;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (opts.min !== undefined) lo = Math.min(lo, opts.min);
  if (opts.max !== undefined) hi = Math.max(hi, opts.max);
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.05; lo -= pad; hi += pad;
  const n = labels.length;
  const x = (i) => padL + (n <= 1 ? 0 : (i / (n - 1)) * W);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * H;

  // gridlines + y labels
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = COLORS.muted;
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  const ticks = 4;
  for (let t = 0; t <= ticks; t++) {
    const v = lo + (hi - lo) * (t / ticks);
    const yy = y(v);
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + W, yy); ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(fmtAxis(v), padL - 6, yy + 3);
  }
  // x labels: first / middle / last
  ctx.textAlign = "center";
  [0, Math.floor((n - 1) / 2), n - 1].forEach(i => {
    if (i >= 0 && i < n) ctx.fillText(String(labels[i]).slice(0, 10), Math.max(padL + 25, Math.min(x(i), cssW - 35)), cssH - 5);
  });

  // reference lines (e.g. RSI 30/70, score 0)
  (opts.refLines || []).forEach(rl => {
    ctx.strokeStyle = rl.color || COLORS.baseline;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padL, y(rl.value)); ctx.lineTo(padL + W, y(rl.value)); ctx.stroke();
    ctx.setLineDash([]);
  });

  // series
  series.forEach(s => {
    if (s.type === "bar") {
      const bw = Math.max(1, W / n - 1);
      s.data.forEach((v, i) => {
        if (v === null || v === undefined) return;
        ctx.fillStyle = s.colorFn ? s.colorFn(v) : s.color;
        const y0 = y(Math.max(0, lo)), y1 = y(v);
        ctx.fillRect(x(i) - bw / 2, Math.min(y0, y1), bw, Math.max(1, Math.abs(y0 - y1)));
      });
      return;
    }
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 2;
    if (s.dash) ctx.setLineDash(s.dash);
    ctx.beginPath();
    let started = false;
    s.data.forEach((v, i) => {
      if (v === null || v === undefined || !isFinite(v)) { started = false; return; }
      if (!started) { ctx.moveTo(x(i), y(v)); started = true; }
      else ctx.lineTo(x(i), y(v));
    });
    ctx.stroke(); ctx.setLineDash([]);
  });

  // legend (only when 2+ line series)
  const lineSeries = series.filter(s => s.type !== "bar" && s.name);
  if (lineSeries.length >= 2) {
    let lx = padL + 6;
    ctx.textAlign = "left"; ctx.font = "11px system-ui, sans-serif";
    lineSeries.forEach(s => {
      ctx.fillStyle = s.color; ctx.fillRect(lx, padT + 2, 10, 3);
      ctx.fillStyle = css("--ink-2") || "#c3c2b7";
      ctx.fillText(s.name, lx + 14, padT + 8);
      lx += 14 + ctx.measureText(s.name).width + 14;
    });
  }

  charts[canvasId] = { labels, series, x, y, padL, W, n, canvas };
  canvas.onmousemove = (e) => chartHover(e, canvasId);
  canvas.onmouseleave = () => { $("#tooltip").hidden = true; };
}

function chartHover(e, canvasId) {
  const c = charts[canvasId];
  if (!c) return;
  const rect = c.canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const i = Math.round((mx - c.padL) / (c.W || 1) * (c.n - 1));
  if (i < 0 || i >= c.n) { $("#tooltip").hidden = true; return; }
  const lines = [String(c.labels[i])];
  c.series.forEach(s => {
    const v = s.data[i];
    if (v !== null && v !== undefined && isFinite(v)) lines.push(`${s.name || "value"}: ${fmtNum(v)}`);
  });
  const tt = $("#tooltip");
  tt.textContent = lines.join("\n");
  tt.hidden = false;
  tt.style.left = Math.min(e.clientX + 14, window.innerWidth - 180) + "px";
  tt.style.top = (e.clientY + 14) + "px";
}

const fmtAxis = (v) => Math.abs(v) >= 1e9 ? (v / 1e9).toFixed(1) + "B"
  : Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + "M"
  : Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + "k"
  : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2);
const fmtNum = (v) => typeof v === "number" ? (Math.abs(v) >= 100 ? v.toFixed(2) : v.toFixed(3)) : v;
const fmt$ = (v) => v === null || v === undefined ? "–" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
const pillClass = (label) => (label || "").replace(/ /g, "-");

function authHeaders() {
  const t = localStorage.getItem("jtoken");
  return t ? { "X-Auth-Token": t } : {};
}

async function api(path, timeoutMs = 45000) {
  // Explicit timeout: without it a slow upstream (Yahoo throttling) leaves
  // Safari hanging until it gives up with an unhelpful bare "Load failed".
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let r;
  try {
    r = await fetch(path, { headers: authHeaders(), signal: ctrl.signal });
  } catch (e) {
    throw new Error(e.name === "AbortError"
      ? "server is responding slowly (market data source may be rate-limiting) — retrying on the next auto-refresh"
      : "can't reach the app server — is it running? (start it with ./run.sh in the trade folder)");
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

/* ---------------- 3-tier understanding level & deep-dive explainer ---------------- */

const WATCHLIST_17 = [
  "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AMD", "AVGO", "PLTR",
  "INTC", "SOFI", "F", "RIVN", "SPY", "QQQ", "IWM"
];

let userLevel = localStorage.getItem("tp_level") || "trader"; // "beginner" | "trader" | "quant"
let lastAgentBrief = null;
let lastAnalysisData = null;
let lastOptionsData = null;
let currentExplainerTopic = "full_setup";
let currentExplainerCtx = {};

const LEVEL_BANNERS = {
  beginner: "🌱 <b>Beginner Mode (Guided Coach):</b> Plain-English translations, exact dollar risk/reward, and step-by-step guidance. Tap any card with <b>ⓘ Explain</b> or press <b>📖 Explain</b> for the full story.",
  trader: "⚡ <b>Trader Mode (Active Swing):</b> Actionable technical setups, ATR stops/targets, and 30–45 DTE option plans. Tap any metric or <b>📖 Explain</b> for a full deep dive.",
  quant: "🔬 <b>Quant Mode (Pro Surface):</b> Full Black-Scholes Greeks, IV percentiles vs. realized vol, factor weights, and statistical backtest diagnostics. Tap any metric to inspect.",
};

function syncLevelUI() {
  document.querySelectorAll("#level-switcher button, .drawer-levels button").forEach(b => {
    b.classList.toggle("active", b.dataset.level === userLevel);
  });
  const banner = $("#level-banner-desc");
  if (banner) banner.innerHTML = LEVEL_BANNERS[userLevel] || LEVEL_BANNERS.trader;
}

function setLevel(lvl) {
  if (!["beginner", "trader", "quant"].includes(lvl)) return;
  userLevel = lvl;
  localStorage.setItem("tp_level", lvl);
  syncLevelUI();
  if (lastAgentBrief && activeTab() === "dashboard") renderAgentBrief(lastAgentBrief);
  if (lastAnalysisData && activeTab() === "analyze") renderAnalysisView(lastAnalysisData);
  if (lastOptionsData && activeTab() === "options") renderOptionsView(lastOptionsData);
  if (activeTab() === "news") renderNews();
  const drawer = $("#detail-drawer");
  if (drawer && !drawer.hidden) renderExplainerBody(currentExplainerTopic, currentExplainerCtx);
}

document.querySelectorAll("#level-switcher button, .drawer-levels button").forEach(btn => {
  btn.addEventListener("click", () => setLevel(btn.dataset.level));
});

/* ---------------- tabs & mobile bottom nav ---------------- */

document.querySelectorAll("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

document.querySelectorAll("#mobile-nav button[data-tab]").forEach(btn => {
  btn.addEventListener("click", () => {
    $("#mobile-more-sheet").hidden = true;
    switchTab(btn.dataset.tab);
  });
});

if ($("#mobile-more-btn")) {
  $("#mobile-more-btn").addEventListener("click", () => {
    const sheet = $("#mobile-more-sheet");
    sheet.hidden = !sheet.hidden;
  });
}
if ($("#mobile-more-close")) {
  $("#mobile-more-close").addEventListener("click", () => {
    $("#mobile-more-sheet").hidden = true;
  });
}
document.querySelectorAll("#mobile-more-sheet button[data-tab]").forEach(btn => {
  btn.addEventListener("click", () => {
    $("#mobile-more-sheet").hidden = true;
    switchTab(btn.dataset.tab);
  });
});

function switchTab(name) {
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  const primaryMobile = ["dashboard", "analyze", "options", "news"];
  document.querySelectorAll("#mobile-nav button[data-tab]").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  const moreBtn = $("#mobile-more-btn");
  if (moreBtn) moreBtn.classList.toggle("active", !primaryMobile.includes(name));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
  loadTab(name);
}

function activeTab() {
  const el = document.querySelector("#tabs button.active");
  return el ? el.dataset.tab : "dashboard";
}

function loadTab(name) {
  if (name === "dashboard") loadWatchlist();
  if (name === "town") loadTown();
  if (name === "agent") loadAgentBody();
  if (name === "scanner") loadScanner();
  if (name === "trend") loadTrend();
  if (name === "analyze") loadAnalysis();
  if (name === "options") loadOptions();
  if (name === "backtest") { $("#bt-ticker").textContent = currentTicker; }
  if (name === "news") loadNews();
  if (name === "journal") loadJournal();
}

$("#go-btn").addEventListener("click", () => {
  currentTicker = $("#ticker-input").value.trim().toUpperCase() || currentTicker;
  const tab = activeTab();
  if (tab === "dashboard") loadAgent(currentTicker);
  else loadTab(tab);
});
$("#ticker-input").addEventListener("keydown", e => { if (e.key === "Enter") $("#go-btn").click(); });

if ($("#explain-all-btn")) {
  $("#explain-all-btn").addEventListener("click", () => {
    openExplainer("full_setup", { ticker: currentTicker, brief: lastAgentBrief, analysis: lastAnalysisData, options: lastOptionsData });
  });
}

/* ---------------- full-detail interactive explainer engine ---------------- */

const EXPLAINER_CATALOG = {
  signal_score: {
    title: "Composite Signal Score (-100 to +100)",
    kicker: "TECHNICAL CONVICTION",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p>Think of the <b>Signal Score</b> as a weather forecast for <b>${c.ticker || currentTicker}</b> on a scale from <b>-100 (stormy downtrend)</b> to <b>+100 (clear uptrend)</b>.</p>
        <p>Right now, <b>${c.ticker || currentTicker}</b> scores <b>${c.val ?? "–"}</b> (${c.label || "Rated"}). Scores above <b>+20</b> mean buyers are pushing the stock higher; below <b>-15</b> means sellers are in control; between -15 and +20 means the stock is choppy so waiting is safer.</p>
      </div>
      <div class="drawer-section">
        <h4>💡 How to Use It</h4>
        <p>· <b>+45 to +100 (STRONG BUY):</b> All major trend and volume checks agree upward.<br>
        · <b>+20 to +44 (BUY):</b> Uptrend is forming, keep position sizes modest.<br>
        · <b>-14 to +19 (NEUTRAL):</b> Sideways tug-of-war — avoid buying expiring options here because time decay eats sideways trades.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Swing Trader Breakdown</h4>
        <p><b>${c.ticker || currentTicker}</b> prints a composite score of <b>${c.val ?? "–"} (${c.label || ""})</b>. This blends 5 indicator groups: <b>Trend (30%)</b>, <b>Momentum (25%)</b>, <b>Volume (20%)</b>, <b>Mean Reversion (15%)</b>, and <b>Volatility (10%)</b>.</p>
        <p>Always check <b>Weekly Confluence</b> alongside the daily score: when both daily and weekly timeframes agree, historical win rates improve significantly.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Quantitative Specification</h4>
        <p>Weighted linear combination mapped to $[-100, +100]$: Trend ($0.30$: SMA20/50/200 stack + slope), Momentum ($0.25$: MACD histogram sign/acceleration + 10d ROC), Volume ($0.20$: 20d relative volume confirmation + OBV slope), Mean-Reversion ($0.15$: RSI-14 + Bollinger %B), and Volatility ($0.10$: BB width squeeze).</p>
      </div>`
  },
  atm_iv: {
    title: "Implied Volatility (ATM IV %)",
    kicker: "OPTION PRICING & VEGA RISK",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p><b>Implied Volatility (IV)</b> tells you whether option contracts on <b>${c.ticker || currentTicker}</b> are currently <b>on sale</b> or <b>marked up like surge-priced Uber rides</b>.</p>
        <p>Current reading: <b>${c.val ?? "–"}%</b>. Higher IV means the market expects big price swings (often right before earnings), making options expensive to buy.</p>
      </div>
      <div class="drawer-section">
        <h4>⚠️ Watch Out for "IV Crush"</h4>
        <p>If you buy an expensive high-IV option right before a big announcement, the stock can move your way and the option can <i>still lose value</i> once the suspense is over and IV drops.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Active Trader Rules</h4>
        <p>At <b>${c.val ?? "–"}% ATM IV</b>: when IV is cheap-to-fair (&lt;50th percentile vs realized volatility), long calls/puts offer clean leverage. When IV is elevated (&gt;70th percentile), prefer <b>vertical debit spreads</b> so the short leg offsets Vega and Theta.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Quantitative Specification</h4>
        <p>Annualized implied standard deviation $\\sigma_{\\text{imp}}$ backed out of the nearest-to-spot strike price via Black-Scholes-Merton. Compare against 20-day historical realized volatility ($\\sigma_{\\text{RV20}} = \\text{std}(\\ln(S_t/S_{t-1}))\\sqrt{252}$) to measure the Volatility Risk Premium (VRP).</p>
      </div>`
  },
  expected_move: {
    title: "Expected Move (±$ Range by Expiry)",
    kicker: "IMPLIED PROBABILITY CONE",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p>The options market is pricing in a <b>${c.val || "±$–"}</b> move for <b>${c.ticker || currentTicker}</b> between now and expiration (~68% one-standard-deviation range).</p>
        <p>If you buy a call option, make sure your <b>breakeven price</b> (Strike + Option Cost) sits comfortably <i>inside</i> this expected move—otherwise you are betting on a rare outlier jump.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Strike &amp; Target Selection</h4>
        <p>Implied move is <b>${c.val || "–"}</b>. Use the upper/lower expected-move boundaries to set realistic profit-taking targets or to pick the short strike on a vertical debit spread.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Formula</h4>
        <p>$$\\text{Expected Move} \\approx S_0 \\cdot \\sigma_{\\text{ATM}} \\cdot \\sqrt{\\frac{\\text{DTE}}{365}}$$ Represents the 1-$\\sigma$ ($\\approx 68.2\\%$) terminal lognormal distribution band under risk-neutral pricing.</p>
      </div>`
  },
  put_call_ratio: {
    title: "Put / Call Ratio (Crowd Positioning)",
    kicker: "OPTIONS FLOW SENTIMENT",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p>Compares how many bearish <b>Put</b> contracts vs. bullish <b>Call</b> contracts are trading on <b>${c.ticker || currentTicker}</b> (Current: <b>${c.val ?? "–"}</b>).</p>
        <p>· <b>Below 0.70:</b> Traders are heavily buying Calls (bullish optimism).<br>
        · <b>0.70 to 1.00:</b> Balanced positioning.<br>
        · <b>Above 1.10:</b> Heavy Put buying (hedging or bearish fear).</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Flow Confirmation vs. Contrarian Extremes</h4>
        <p>Volume P/C captures today's urgency; Open Interest P/C captures sticky multi-day positioning. Sub-0.75 confirms bullish breakouts; extreme spikes (&gt;1.5) near support often mark capitulation bottoms.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Flow Ratio</h4>
        <p>$\\text{PCR}_{\\text{vol}} = \\frac{\\sum V_{\\text{put}}}{\\sum V_{\\text{call}}}$ across all strikes in the selected expiration series.</p>
      </div>`
  },
  max_pain: {
    title: "Max Pain Strike Price",
    kicker: "EXPIRATION PINNING MAGNET",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p><b>Max Pain (${c.val ? fmt$(c.val) : "–"})</b> is the stock price where the largest number of open option contracts would expire worthless.</p>
        <p>It matters most in the <b>final 2 to 5 days before expiration</b>, when market makers hedging their books can create a mild "gravity pull" toward this price.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ How Traders Use Max Pain</h4>
        <p>Treat Max Pain (<b>${c.val ? fmt$(c.val) : "–"}</b>) as a secondary reference during OpEx week when dealer gamma hedging is largest, never as a standalone directional trigger at 30+ DTE.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Objective Function</h4>
        <p>$$K^* = \\arg\\min_K \\sum_i \\left( \\text{OI}_{\\text{call},i}\\max(0, K - K_i) + \\text{OI}_{\\text{put},i}\\max(0, K_i - K) \\right)$$</p>
      </div>`
  },
  earnings: {
    title: "Earnings Catalyst Countdown",
    kicker: "BINARY EVENT RISK",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p>Next earnings date for <b>${c.ticker || currentTicker}</b>: <b>${c.val || "–"}</b>.</p>
        <p>Holding a basic call or put option overnight through an earnings report is like flipping a coin at a casino table with inflated ticket prices: even if you guess the direction right, <b>IV Crush</b> can wipe out 30–60% of the option's value at the 9:30 AM open.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Pre-Earnings Rulebook</h4>
        <p>If earnings is within <b>14 days</b>, either exit directional swing options <i>before</i> the bell before the report, or structure the trade as a defined-risk vertical spread.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Term Structure Jump</h4>
        <p>Front-month implied variance decomposes into diffusion variance plus a discrete earnings jump variance $\\sigma_{\\text{earn}}^2$. Post-announcement, $\\sigma_{\\text{earn}}^2 \\to 0$ instantaneously.</p>
      </div>`
  },
  win_rate: {
    title: "Historical Backtest Win Rate & Track Record",
    kicker: "EMPIRICAL VALIDATION",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p>Before risking money, this app tests its own signal rules on 2–3 years of past <b>${c.ticker || currentTicker}</b> price history. A <b>${c.val ?? "–"}</b> win rate shows what percentage of past setups on this exact stock finished profitable.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Evaluating Win Rate With Profit Factor</h4>
        <p>Win rate (<b>${c.val ?? "–"}</b>) must be read alongside <b>Profit Factor</b> and <b>Average Win vs. Average Loss</b>. With a 2:1 reward-to-risk target ($3\\times\\text{ATR}$ target vs $1.5\\times\\text{ATR}$ stop), even a 45% win rate is solidly profitable.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Expectancy Identity</h4>
        <p>$$E[R] = p_{\\text{win}}\\cdot \\overline{R}_{\\text{win}} - (1 - p_{\\text{win}})\\cdot |\\overline{R}_{\\text{loss}}|$$</p>
      </div>`
  },
  profit_factor: {
    title: "Profit Factor (Gross Wins ÷ Gross Losses)",
    kicker: "RISK EFFICIENCY",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p><b>Profit Factor (${c.val ?? "–"})</b> tells you how many dollars the strategy made on winning trades for every $1.00 it lost on losing trades.</p>
        <p>· <b>Below 1.0:</b> Lost money overall.<br>· <b>1.2 to 1.5:</b> Solid edge.<br>· <b>Above 1.6:</b> Strong historical edge on this ticker.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Strategy Filter</h4>
        <p>Prefer tickers where Profit Factor &ge; 1.25 over at least 10 historical trades.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Definition</h4>
        <p>$\\text{PF} = \\frac{\\sum_{r_i > 0} r_i}{\\sum_{r_j < 0} |r_j|}$ across all closed out-of-bar backtest trades.</p>
      </div>`
  },
  dte: {
    title: "Days to Expiration (DTE) & Time Decay (Theta)",
    kicker: "THE 30–45 DTE RULE",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Plain-English Meaning</h4>
        <p><b>DTE (${c.val ?? "–"} days)</b> is the countdown clock until the option expires. Every option loses a slice of value each day called <b>Theta (Time Decay)</b>.</p>
        <p>Options with <b>30 to 45 days left</b> decay slowly (~1–2% per day), giving your trade time to work. Options under <b>14 days</b> melt like ice cubes on hot pavement.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Sweet-Spot Mechanics</h4>
        <p>Enter directional swings at <b>30–45 DTE</b> and plan to exit or roll before <b>14–21 DTE</b> so you hold during the flat part of the Theta curve and exit before the steep decay cliff.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Theta Scaling</h4>
        <p>At-the-money Black-Scholes Theta scales as $\\Theta_{\\text{ATM}} \\propto -\\frac{S\\sigma}{2\\sqrt{T}}$. As $T \\to 0$, daily decay diverges as $T^{-1/2}$.</p>
      </div>`
  },
  headline: {
    title: "Live News Sentiment & Pre-Trade Gate Impact",
    kicker: "CATALYST & SENTIMENT ENGINE",
    beginner: (c) => `
      <div class="drawer-section">
        <h4>🌱 Why This Headline Matters</h4>
        <p><b>Headline:</b> "${esc(c.title || "Market news feed")}"</p>
        <p><b>Detected Tone:</b> <span class="pill ${c.sentiment === "positive" ? "BUY" : c.sentiment === "negative" ? "SELL" : "NEUTRAL"}">${(c.sentiment || "neutral").toUpperCase()}</span>
        ${(c.words && c.words.length) ? ` (Trigger words: <b>${esc(c.words.join(", "))}</b>)` : ""}</p>
        <p>Single headlines create short-term noise, but when <b>2 or more negative headlines</b> hit the same stock within a few hours (a <b>News Cluster</b>), our Analyst agent automatically raises a caution flag so you don't buy into a falling knife.</p>
      </div>`,
    trader: (c) => `
      <div class="drawer-section">
        <h4>⚡ Catalyst &amp; Gate Rules</h4>
        <p><b>Headline:</b> "${esc(c.title || "Market news feed")}" (${esc(c.source || "Feed")})</p>
        <p>Tagged Tickers: <b>${(c.tickers && c.tickers.length) ? c.tickers.join(", ") : "Broad Market / Macro"}</b> · Matched Stems: <b>${(c.words && c.words.length) ? c.words.join(", ") : "None (Neutral)"}</b>.</p>
        <p>Use positive news clusters to confirm high-volume technical breakouts; treat negative clusters as an immediate hard veto on new long calls.</p>
      </div>`,
    quant: (c) => `
      <div class="drawer-section">
        <h4>🔬 Inflected Stem &amp; LLM Hybrid Classifier</h4>
        <p>Deterministic regex stemmer (<code class="font-mono">app/news.py</code>) catches inflected present/progressive/past financial verbs across 17 watchlist regex patterns, supplemented by Claude Haiku semantic rescoring (<code class="font-mono">app/llmsentiment.py</code>) and enforced by the 12-hour standing alert window in <code class="font-mono">app/gate.py</code>.</p>
      </div>`
  }
};

function renderFullSetupWalkthrough(ctx) {
  const tk = ctx.ticker || currentTicker;
  const b = ctx.brief || lastAgentBrief;
  if (!b || !b.signal) {
    return `
      <div class="drawer-section">
        <h4>Loading ${esc(tk)} Setup…</h4>
        <p>Select a ticker on the Dashboard or click <b>Analyze</b> so we can build a complete personalized walkthrough using live market numbers.</p>
      </div>`;
  }
  const sig = b.signal || {};
  const opt = b.options || {};
  const rec = opt.recommendation || {};
  const sug = rec.suggestion;
  const bt = b.backtest || {};
  const px = b.quote?.price;
  const atr = sig.atr || (px ? +(px * 0.025).toFixed(2) : 1);
  const stopPx = px ? (sig.score >= 0 ? +(px - 1.5 * atr).toFixed(2) : +(px + 1.5 * atr).toFixed(2)) : null;
  const targetPx = px ? (sig.score >= 0 ? +(px + 3.0 * atr).toFixed(2) : +(px - 3.0 * atr).toFixed(2)) : null;
  const contractCost = sug?.est_mid_price ? +(sug.est_mid_price * 100).toFixed(0) : null;
  const pctMeter = Math.max(5, Math.min(95, Math.round((sig.score + 100) / 2)));

  if (userLevel === "beginner") {
    return `
      <div class="drawer-section">
        <h4>1 · Big-Picture Verdict for ${esc(tk)} (${fmt$(px)})</h4>
        <p>Right now <b>${esc(tk)}</b> has a score of <b>${sig.score >= 0 ? "+" : ""}${sig.score} (${sig.label})</b>.</p>
        <div class="meter-bar"><div class="meter-fill" style="width:${pctMeter}%"></div></div>
        <p>${sig.score >= 20
          ? `Buyers are pushing <b>${esc(tk)}</b> upward. The trend and momentum favor looking for a <b>bullish (upward)</b> setup.`
          : sig.score <= -15
          ? `Sellers are pushing <b>${esc(tk)}</b> downward. Avoid buying calls against a falling trend; only bearish setups make sense here.`
          : `<b>${esc(tk)}</b> is moving sideways in a tug-of-war. When a stock has no clear trend, the smartest move for a beginner is to <b>wait on the sidelines</b> so option time decay doesn't slowly drain your cash.`}</p>
      </div>
      <div class="drawer-section">
        <h4>2 · Stock Price Guardrails (Where to Exit)</h4>
        <p>Never enter a trade without knowing two prices in advance:<br>
        · <b>Safety Stop Price (${fmt$(stopPx)}):</b> Based on ${esc(tk)}'s normal daily bounce (ATR ${fmt$(sig.atr)}), if the stock crosses ${fmt$(stopPx)}, the setup is broken—exit to protect your capital.<br>
        · <b>Profit Target (${fmt$(targetPx)}):</b> A realistic 2-to-1 reward target where you lock in gains.</p>
      </div>
      <div class="drawer-section">
        <h4>3 · Options Plan in Real Dollars</h4>
        ${sug ? `<p><b>Suggested Contract:</b> ${esc(sug.action)} <b>${esc(tk)} $${sug.strike}</b> expiring <b>${esc(sug.expiry)}</b> (${sug.dte} days left).</p>
        <p>· <b>Cost for 1 Contract (100 shares):</b> ~<b>$${contractCost}</b> (${fmt$(sug.est_mid_price)} × 100). This is the maximum you can lose.<br>
        · <b>Win Chance / Delta (${sug.delta}):</b> Moves ~${fmt$(Math.abs(sug.delta || 0.5) * 100)} for every $1 move in ${esc(tk)} stock.<br>
        · <b>Why ${sug.dte} Days?</b> Gives the trade time to work before steep time decay kicks in.</p>`
        : `<p>Because the signal or volatility setup doesn't offer a clean edge right now, the engine recommends <b>NO OPTION TRADE</b> today. Preserving cash is a win.</p>`}
      </div>
      <div class="drawer-section">
        <h4>4 · Historical Track Record &amp; News Check</h4>
        <p>· <b>Past Win Rate on ${esc(tk)}:</b> <b>${bt.win_rate ?? "–"}%</b> over ${bt.num_trades ?? 0} past trades (Profit Factor: <b>${bt.profit_factor ?? "–"}</b>).<br>
        · <b>Next Earnings:</b> <b>${b.earnings?.next_earnings ?? "None soon"}</b> ${b.earnings?.days_until != null ? `(${b.earnings.days_until} days away)` : ""}.</p>
      </div>`;
  }

  if (userLevel === "trader") {
    return `
      <div class="drawer-section">
        <h4>1 · Multi-Timeframe &amp; Signal Read (${esc(tk)} @ ${fmt$(px)})</h4>
        <p>Daily Composite: <b>${sig.score >= 0 ? "+" : ""}${sig.score} (${sig.label})</b> · Weekly Confluence: <b>${b.confluence?.weekly_label || "–"} (${b.confluence?.state || "–"})</b> · RSI(14): <b>${sig.rsi ?? "–"}</b> · ATR(14): <b>${fmt$(sig.atr)}</b>.</p>
        <div class="meter-bar"><div class="meter-fill" style="width:${pctMeter}%"></div></div>
        <p><b>Key Indicator Votes:</b></p>
        <ul class="reasons">${(sig.votes || []).map(v => `<li><b>${esc(v.component)} (${v.score >= 0 ? "+" : ""}${v.score}):</b> ${esc(v.reason)}</li>`).join("")}</ul>
      </div>
      <div class="drawer-section">
        <h4>2 · Risk Geometry &amp; Options Structure</h4>
        <p>· <b>Underlying Stop (1.5×ATR):</b> ${fmt$(stopPx)} · <b>Target (3.0×ATR):</b> ${fmt$(targetPx)} (2.0R).<br>
        · <b>ATM IV:</b> ${opt.atm_iv ?? "–"}% (${opt.iv_context?.label || "–"}, ${opt.iv_context?.percentile ?? "–"}th pctile vs RV20) · <b>Expected Move:</b> ±$${opt.expected_move ?? "–"} (±${opt.expected_move_pct ?? "–"}%).</p>
        ${sug ? `<p><b>Primary Contract:</b> ${esc(sug.action)} $${sug.strike} exp ${esc(sug.expiry)} (${sug.dte} DTE, Δ ${sug.delta}, mid ${fmt$(sug.est_mid_price)} = ~$${contractCost}/contract).</p>` : ""}
        ${rec.spread ? `<p><b>Defined-Risk Spread:</b> ${esc(rec.spread.name)} ($${rec.spread.buy_strike}/$${rec.spread.sell_strike}) for ${fmt$(rec.spread.net_debit)} debit (${rec.spread.reward_risk}:1 R:R, BE $${rec.spread.breakeven}).</p>` : ""}
      </div>
      <div class="drawer-section">
        <h4>3 · Empirical Scorecard &amp; Catalysts</h4>
        <p>2Y Backtest: <b>${bt.win_rate ?? "–"}% win rate</b> across ${bt.num_trades ?? 0} trades, Profit Factor <b>${bt.profit_factor ?? "–"}</b>, Max Drawdown <b>${bt.max_drawdown_pct ?? "–"}%</b>. Earnings in <b>${b.earnings?.days_until ?? "–"} days</b>.</p>
      </div>`;
  }

  return `
    <div class="drawer-section">
      <h4>1 · Factor Decomposition (${esc(tk)})</h4>
      <p>Composite $S = ${sig.score}$ · Spot $S_0 = ${fmt$(px)}$ · $\\text{ATR}_{14} = ${fmt$(sig.atr)}$ · $\\text{RSI}_{14} = ${sig.rsi ?? "–"}$.</p>
      <ul class="reasons">${(sig.votes || []).map(v => `<li><code>${esc(v.component)}</code> ($w=${v.weight}$): raw=${v.score} — ${esc(v.reason)}</li>`).join("")}</ul>
    </div>
    <div class="drawer-section">
      <h4>2 · Volatility Surface &amp; BSM Greeks</h4>
      <p>$\\sigma_{\\text{ATM}} = ${opt.atm_iv ?? "–"}\\%$ · RV Percentile = ${opt.iv_context?.percentile ?? "–"}% (${opt.iv_context?.label || "–"}) · 25$\\Delta$-style Skew = ${opt.iv_skew ?? "–"} vol pts · PCR(vol) = ${opt.put_call_ratio_volume ?? "–"} · PCR(OI) = ${opt.put_call_ratio_oi ?? "–"} · Max Pain $K^* = ${fmt$(opt.max_pain)}$.</p>
      ${sug ? `<p>Selected Strike $K=${sug.strike}$, $T=${sug.dte}/365$, $\\Delta=${sug.delta}$, $\\Theta=${sug.theta ?? "–"}$/day, Mid=${fmt$(sug.est_mid_price)}.</p>` : ""}
    </div>
    <div class="drawer-section">
      <h4>3 · Out-of-Sample / Historical Expectancy</h4>
      <p>Trades $N=${bt.num_trades ?? 0}$, Win Rate $p=${bt.win_rate ?? "–"}\\%$, Profit Factor $\\text{PF}=${bt.profit_factor ?? "–"}$, Expectancy/trade $= ${bt.expectancy_pct ?? "–"}\\%$, Max DD $= ${bt.max_drawdown_pct ?? "–"}\\%$.</p>
    </div>`;
}

function renderExplainerBody(topicKey, ctx = {}) {
  const body = $("#drawer-body");
  const titleEl = $("#drawer-title");
  const kickerEl = $("#drawer-kicker");
  if (!body) return;

  if (topicKey === "full_setup" || !EXPLAINER_CATALOG[topicKey]) {
    kickerEl.textContent = `FULL-DETAIL WALKTHROUGH · ${userLevel.toUpperCase()} MODE`;
    titleEl.textContent = `${ctx.ticker || currentTicker} — Complete Setup Explained`;
    body.innerHTML = renderFullSetupWalkthrough(ctx);
    return;
  }

  const item = EXPLAINER_CATALOG[topicKey];
  kickerEl.textContent = `${item.kicker} · ${userLevel.toUpperCase()} MODE`;
  titleEl.textContent = item.title;
  const fn = item[userLevel] || item.trader;
  body.innerHTML = fn(ctx) + `
    <div class="drawer-section">
      <h4>Want the Full ${esc(ctx.ticker || currentTicker)} Walkthrough?</h4>
      <p>See how this metric fits into the complete trading plan for <b>${esc(ctx.ticker || currentTicker)}</b>.</p>
      <button type="button" class="explain-btn" onclick="openExplainer('full_setup', {ticker: '${esc(ctx.ticker || currentTicker)}'})">📖 Open Complete ${esc(ctx.ticker || currentTicker)} Walkthrough</button>
    </div>`;
}

function openExplainer(topicKey, ctx = {}) {
  currentExplainerTopic = topicKey;
  currentExplainerCtx = ctx;
  syncLevelUI();
  renderExplainerBody(topicKey, ctx);
  $("#detail-backdrop").hidden = false;
  const drawer = $("#detail-drawer");
  drawer.hidden = false;
  drawer.setAttribute("aria-hidden", "false");
}
window.openExplainer = openExplainer;

function closeExplainer() {
  $("#detail-backdrop").hidden = true;
  const drawer = $("#detail-drawer");
  drawer.hidden = true;
  drawer.setAttribute("aria-hidden", "true");
}
if ($("#drawer-close")) $("#drawer-close").addEventListener("click", closeExplainer);
if ($("#detail-backdrop")) $("#detail-backdrop").addEventListener("click", closeExplainer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeExplainer(); });

function bindExplainableElements(root = document) {
  root.querySelectorAll("[data-explain]").forEach(el => {
    if (el.dataset.boundExplain) return;
    el.dataset.boundExplain = "1";
    el.addEventListener("click", (e) => {
      if (e.target.tagName === "A") return;
      e.stopPropagation();
      let ctx = {};
      try { ctx = JSON.parse(el.dataset.ctx || "{}"); } catch {}
      openExplainer(el.dataset.explain, ctx);
    });
  });
}

function buildCoachBanner(d) {
  const sig = d.signal || {}, opt = d.options || {}, rec = opt.recommendation || {}, sug = rec.suggestion;
  const px = d.quote?.price;
  const cost = sug?.est_mid_price ? Math.round(sug.est_mid_price * 100) : null;

  if (userLevel === "beginner") {
    const verdictText = sig.score >= 20
      ? `<b>${d.ticker} is in an Uptrend (+${sig.score}).</b> Buyers are in control. ${sug ? `If you want to trade this with options, 1 contract of the <b>$${sug.strike} Call (${sug.dte} days left)</b> costs about <b>$${cost} total</b>—and $${cost} is the most you can lose.` : `However, options are currently too expensive or risky near earnings, so waiting or trading shares is safer.`}`
      : sig.score <= -15
      ? `<b>${d.ticker} is in a Downtrend (${sig.score}).</b> Sellers are pushing the price lower—avoid buying Call options into a falling trend.`
      : `<b>${d.ticker} is Sideways / Neutral (${sig.score}).</b> Neither buyers nor sellers have a clear edge today. Since options lose value every day from time decay, <b>sitting on your hands is the smartest play right now.</b>`;
    return `
      <div class="coach-card">
        <div class="coach-head">
          <span class="coach-badge">🌱 Guided Beginner Coach · Plain-English Summary</span>
          <button type="button" class="explain-btn" onclick="openExplainer('full_setup', {ticker:'${d.ticker}'})">📖 Explain Full Setup Step-by-Step</button>
        </div>
        <div class="coach-body">${verdictText} <span class="muted">(Tap any metric tile below to learn what it means!)</span></div>
      </div>`;
  }
  if (userLevel === "trader") {
    return `
      <div class="coach-card">
        <div class="coach-head">
          <span class="coach-badge">⚡ Active Swing Trader Summary</span>
          <button type="button" class="explain-btn" onclick="openExplainer('full_setup', {ticker:'${d.ticker}'})">📖 Full Setup &amp; Risk Breakdown</button>
        </div>
        <div class="coach-body">
          <b>${d.ticker} (${fmt$(px)})</b> scores <b>${sig.score >= 0 ? "+" : ""}${sig.score} (${sig.label})</b> with weekly confluence <b>${d.confluence?.state || "n/a"}</b>.
          ${sug ? `Primary option structure: <b>${sug.action} $${sug.strike} (${sug.dte} DTE, Δ ${sug.delta}, ~${fmt$(sug.est_mid_price)})</b>.` : `Options recommendation: <b>${rec.bias || "NO TRADE"}</b>.`}
          Tap any tile below for rules &amp; edge cases.
        </div>
      </div>`;
  }
  return `
    <div class="coach-card">
      <div class="coach-head">
        <span class="coach-badge">🔬 Quant Surface &amp; Factor Summary</span>
        <button type="button" class="explain-btn" onclick="openExplainer('full_setup', {ticker:'${d.ticker}'})">📖 Inspect Factor &amp; BSM Diagnostics</button>
      </div>
      <div class="coach-body">
        Composite $S = ${sig.score}$ · $\\sigma_{\\text{ATM}} = ${opt.atm_iv ?? "–"}\\%$ (${opt.iv_context?.percentile ?? "–"}th pctile vs RV20) · Skew = ${opt.iv_skew ?? "–"} pts · 1$\\sigma$ Move = ±$${opt.expected_move ?? "–"} (${opt.expected_move_pct ?? "–"}%).
      </div>
    </div>`;
}

/* ---------------- dashboard ---------------- */

async function loadWatchlist() {
  try {
    const d = await api("/api/watchlist");
    const tbody = $("#watchlist-table tbody");
    tbody.innerHTML = "";
    d.quotes.forEach(q => {
      const tr = document.createElement("tr");
      if (q.error) { tr.innerHTML = `<td>${q.ticker}</td><td colspan="6" class="err">${q.error}</td>`; }
      else {
        const cls = (q.change ?? 0) >= 0 ? "up" : "down";
        tr.innerHTML = `<td><b>${q.ticker}</b></td>
          <td>${fmt$(q.price)}</td>
          <td class="${cls}">${q.change === null ? "–" : (q.change >= 0 ? "+" : "") + q.change.toFixed(2)}</td>
          <td class="${cls}">${q.change_pct === null ? "–" : (q.change_pct >= 0 ? "+" : "") + q.change_pct.toFixed(2)}%</td>
          <td class="muted">${fmt$(q.day_low)} – ${fmt$(q.day_high)}</td>
          <td>${q.signal_score ?? "–"}</td>
          <td><span class="pill ${pillClass(q.signal_label)}">${q.signal_label}</span></td>`;
        tr.addEventListener("click", () => { currentTicker = q.ticker; $("#ticker-input").value = q.ticker; loadAgent(q.ticker); });
      }
      tbody.appendChild(tr);
    });
    $("#wl-updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#watchlist-table tbody").innerHTML = `<tr><td colspan="7" class="err">Failed to load: ${e.message}</td></tr>`;
  }
}

function renderHeadlineRow(n) {
  const words = n.sentiment_words || [];
  const wordChip = words.length && n.sentiment !== "neutral"
    ? `<span class="sent-chip ${n.sentiment === "positive" ? "pos" : "neg"}">${n.sentiment === "positive" ? "bullish" : "bearish"}: ${esc(words.slice(0, 3).join(", "))}</span>`
    : "";
  const ctxJson = esc(JSON.stringify({
    title: n.title,
    sentiment: n.sentiment,
    words: words,
    source: n.source,
    tickers: n.tickers || []
  }));
  return `<div class="news-item">
    <span class="dot dot-${n.sentiment === "positive" ? "pos" : n.sentiment === "negative" ? "neg" : "neu"}"></span>
    <a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a>
    ${(n.tickers || []).map(t => `<span class="chip">${esc(t)}</span>`).join("")}
    ${wordChip}
    <span class="news-meta">${esc(n.source)}${n.published_str ? " · " + esc(n.published_str) + " UTC" : ""}</span>
    <button type="button" class="news-why-btn" data-explain="headline" data-ctx="${ctxJson}">ⓘ Why it matters</button>
  </div>`;
}

function renderAgentBrief(d) {
  const box = $("#agent-brief");
  const sig = d.signal, opt = d.options || {}, bt = d.backtest || {}, rec = opt.recommendation || {};
  const isBeg = userLevel === "beginner";
  const lbl = (begText, stdText) => isBeg ? begText : stdText;

  box.innerHTML = `
    ${buildCoachBanner(d)}
    <div class="signal-row">
      <span class="score-big">${d.ticker}</span>
      <span class="score-big ${sig.score >= 0 ? "up" : "down"}" data-explain="signal_score" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: sig.score, label: sig.label}))}" style="cursor:pointer" title="Tap to explain score">${sig.score >= 0 ? "+" : ""}${sig.score}</span>
      <span class="pill ${pillClass(sig.label)}">${sig.label}</span>
      <span class="pill ${pillClass(rec.bias || "NO-TRADE")}">Options bias: ${rec.bias || "n/a"}</span>
      ${d.confluence ? `<span class="${d.confluence.state === "agree" ? "up" : d.confluence.state === "conflict" ? "down" : "muted"}">weekly: ${d.confluence.weekly_label} (${d.confluence.state})</span>` : ""}
      <span class="muted">price ${fmt$(d.quote.price)} (${d.quote.change_pct >= 0 ? "+" : ""}${d.quote.change_pct?.toFixed(2)}%)</span>
    </div>
    ${(rec.warnings || []).map(w => `<div class="warn-box">⚠ ${w}</div>`).join("")}
    <div class="tiles">
      <div class="tile" data-explain="earnings" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: d.earnings?.next_earnings || 'None scheduled'}))}"><div class="k">${lbl("Earnings Risk Date", "Next earnings")}</div><div class="v ${d.earnings?.days_until != null && d.earnings.days_until <= 14 ? "warn-text" : ""}">${d.earnings?.next_earnings ?? "–"}${d.earnings?.days_until != null ? ` <span class="muted">(${d.earnings.days_until}d)</span>` : ""}</div></div>
      <div class="tile" data-explain="atm_iv" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: opt.atm_iv}))}"><div class="k">${lbl("Option Price Level (IV)", "ATM IV")}</div><div class="v">${opt.atm_iv ?? "–"}%</div></div>
      <div class="tile" data-explain="expected_move" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: '±$' + (opt.expected_move ?? '–') + ' (' + (opt.expected_move_pct ?? '–') + '%)'}))}"><div class="k">${lbl("Market's ± Range", `Expected move (${opt.dte ?? "?"} DTE)`)}</div><div class="v">±${opt.expected_move ?? "–"} (${opt.expected_move_pct ?? "–"}%)</div></div>
      <div class="tile" data-explain="put_call_ratio" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: opt.put_call_ratio_volume}))}"><div class="k">${lbl("Put vs Call Crowd", "Put/Call vol")}</div><div class="v">${opt.put_call_ratio_volume ?? "–"}</div></div>
      <div class="tile" data-explain="max_pain" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: opt.max_pain}))}"><div class="k">${lbl("Option Pin Price", "Max pain")}</div><div class="v">${opt.max_pain ?? "–"}</div></div>
      <div class="tile" data-explain="win_rate" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: (bt.win_rate ?? '–') + '%'}))}"><div class="k">${lbl("Past Win Rate (2y)", "Backtest win rate")}</div><div class="v">${bt.win_rate ?? "–"}%</div></div>
      <div class="tile" data-explain="profit_factor" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: bt.profit_factor}))}"><div class="k">${lbl("Win/Loss $ Ratio", "Profit factor")}</div><div class="v">${bt.profit_factor ?? "–"}</div></div>
      ${d.scorecard?.buy?.count ? `<div class="tile" data-explain="win_rate" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: d.scorecard.buy.win_rate + '%'}))}"><div class="k">Buy-signal track record (3y)</div>
        <div class="v ${d.scorecard.buy.win_rate >= 55 ? "up" : d.scorecard.buy.win_rate <= 45 ? "down" : ""}">${d.scorecard.buy.win_rate}%</div>
        <div class="muted">${d.scorecard.buy.count} signals</div></div>` : ""}
      ${opt.iv_context ? `<div class="tile" data-explain="atm_iv" data-ctx="${esc(JSON.stringify({ticker: d.ticker, val: opt.atm_iv}))}"><div class="k">IV context</div><div class="v">${opt.iv_context.label}</div>
        <div class="muted">${opt.iv_context.percentile}th pctile vs realized</div></div>` : ""}
    </div>
    ${rec.spread ? `<p><b>Defined-risk alternative — ${rec.spread.name}:</b> buy $${rec.spread.buy_strike} / sell $${rec.spread.sell_strike},
      max loss ${fmt$(rec.spread.max_loss)}, max profit ${fmt$(rec.spread.max_profit)} (${rec.spread.reward_risk}:1), breakeven ${rec.spread.breakeven}</p>` : ""}
    ${rec.suggestion ? `<p><b>Suggested contract:</b> ${rec.suggestion.action} ${d.ticker} $${rec.suggestion.strike} exp ${rec.suggestion.expiry}
      (~${rec.suggestion.dte} DTE, Δ ${rec.suggestion.delta}, IV ${rec.suggestion.iv}%, mid ≈ ${fmt$(rec.suggestion.est_mid_price)})<br>
      <span class="muted">${rec.suggestion.note}</span></p>` : ""}
    <h3>Trading plan</h3>
    <ul class="reasons">${(d.plan.steps || []).map(s => `<li>${s}</li>`).join("")}</ul>
    <h3 style="margin-top:12px">Why</h3>
    <ul class="reasons">${(rec.reasons || sig.votes.map(v => v.reason)).map(r => `<li>${r}</li>`).join("")}</ul>
    <h3 style="margin-top:12px">Latest ${d.ticker} headlines</h3>
    ${(d.news || []).slice(0, 6).map(renderHeadlineRow).join("") || '<p class="muted">No headlines found.</p>'}
    ${(d.degraded || []).length ? `<p class="muted">Some sections were unavailable this run
      (${d.degraded.join(", ")}) — usually Yahoo rate-limiting. Everything else above is current;
      retry in a minute for the rest.</p>` : ""}
    <p class="muted">${d.plan.disclaimer}</p>`;
  bindExplainableElements(box);
}

async function loadAgent(ticker) {
  const box = $("#agent-brief");
  box.innerHTML = `<p class="spin">Building agent brief for ${ticker} (signal + options + backtest + news)…</p>`;
  try {
    const d = await api("/api/agent/" + ticker);
    lastAgentBrief = d;
    renderAgentBrief(d);
  } catch (e) {
    box.innerHTML = `<p class="err">Agent brief failed: ${e.message}</p>
      <p class="muted">This is almost always Yahoo Finance throttling the server rather than a bug
      in the app. Wait about a minute and click the ticker again — the Dashboard, Analyze and Learn
      tabs keep working from cache meanwhile.</p>
      <button onclick="loadAgent('${ticker}')">Retry now</button>`;
  }
}

/* ---------------- agent body (the visible agent) ---------------- */
/* Mood + face are derived from the tracking store: latest directional call
   sets the stance, recency sets awake/asleep, and the report card is the
   graded hit rate. Everything renders from /api/tracking/*. */

const MOUTHS = {
  bullish: "M78 112 q22 16 44 0",
  bearish: "M78 122 q22 -14 44 0",
  neutral: "M82 117 h36",
  asleep: "M92 116 q8 5 16 0",
};
const DIRECTIONAL_UP = ["buy", "call"], DIRECTIONAL_DOWN = ["sell", "put"];
const ACTION_PILL = { buy: "BUY", call: "CALL", sell: "SELL", put: "PUT",
  hold: "NEUTRAL", no_trade: "NO-TRADE" };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (!isFinite(s)) return ts;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 172800) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}

async function loadAgentBody() {
  try {
    const [s, ev, dec] = await Promise.all([
      api("/api/tracking/summary?days=30"),
      api("/api/tracking/events?limit=25"),
      api("/api/tracking/decisions?limit=30"),
    ]);
    renderAgentBody(s, ev.events, dec.decisions);
    $("#ag-updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#ag-headline").textContent = "I can't reach my memory.";
    $("#ag-sub").textContent = e.message + " — is the server running next to journal.db?";
  }
}

function agentState(s, decisions) {
  // Shared brain-state: the Agent tab's face and the Neighborhood's house
  // must always agree on whether the agent is awake and which way it leans.
  const lastTs = (s.events && s.events.last_ts) || (decisions[0] && decisions[0].ts) || null;
  const hoursSince = lastTs ? (Date.now() - new Date(lastTs).getTime()) / 3600000 : Infinity;
  const lastDir = decisions.find(d =>
    DIRECTIONAL_UP.includes(d.action) || DIRECTIONAL_DOWN.includes(d.action));
  const dirFresh = lastDir && (Date.now() - new Date(lastDir.ts).getTime()) < 3 * 86400000;
  let mood = "neutral";
  if (hoursSince > 24) mood = "asleep";
  else if (dirFresh) mood = DIRECTIONAL_UP.includes(lastDir.action) ? "bullish" : "bearish";
  return { mood, lastTs, lastDir };
}

function renderAgentBody(s, events, decisions) {
  const tr = s.track_record || {};
  const { mood, lastTs, lastDir } = agentState(s, decisions);

  $("#agent-svg").setAttribute("class", "mood-" + mood);
  $("#agent-mouth").setAttribute("d", MOUTHS[mood]);

  // Speech bubble
  const hl = $("#ag-headline"), sub = $("#ag-sub");
  if (!lastTs) {
    hl.textContent = "Nobody has woken me up yet.";
    sub.innerHTML = "Open this repo in Claude Code (<code>claude</code> in the folder), connect " +
      "Robinhood with <code>/mcp</code>, and talk to me — my first analysis will appear here.";
  } else if (mood === "asleep") {
    hl.textContent = "Asleep — last active " + fmtAgo(lastTs) + ".";
    sub.textContent = "Start a Claude Code session in the repo folder to put me back to work.";
  } else if (mood === "bullish" || mood === "bearish") {
    hl.textContent = `Leaning ${mood} — my latest call is ${lastDir.action.toUpperCase()} ${lastDir.ticker}.`;
    sub.textContent = `Made ${fmtAgo(lastDir.ts)}` +
      (lastDir.score !== null && lastDir.score !== undefined ? ` at signal score ${lastDir.score > 0 ? "+" : ""}${lastDir.score}` : "") +
      (lastDir.rationale ? ` — ${lastDir.rationale}` : "") + ".";
  } else {
    hl.textContent = "Awake and watching — no strong stance right now.";
    sub.textContent = "Last activity " + fmtAgo(lastTs) +
      ". Neutral tape is a position too: the best trade is often no trade.";
  }
  const pills = [];
  if (lastDir) pills.push(`<span class="pill ${ACTION_PILL[lastDir.action]}">${esc(lastDir.action)} ${esc(lastDir.ticker)}</span>`);
  if (tr.graded) pills.push(`<span class="pill ${tr.hit_rate >= 55 ? "BUY" : tr.hit_rate <= 45 ? "SELL" : "NEUTRAL"}">hit rate ${tr.hit_rate}%</span>`);
  if (s.decisions && s.decisions.pending) pills.push(`<span class="chip">${s.decisions.pending} call(s) awaiting grade</span>`);
  $("#ag-pills").innerHTML = pills.join(" ");

  // Vitals
  $("#ag-tiles").innerHTML = [
    ["Actions (30d)", s.events ? s.events.total : 0],
    ["Decisions", s.decisions ? s.decisions.total : 0],
    ["Awaiting grade", s.decisions ? s.decisions.pending : 0],
    ["Hit rate", tr.hit_rate !== null && tr.hit_rate !== undefined ? tr.hit_rate + "%" : "–"],
    ["Avg fwd return", tr.avg_fwd_return_pct !== null && tr.avg_fwd_return_pct !== undefined
      ? (tr.avg_fwd_return_pct >= 0 ? "+" : "") + tr.avg_fwd_return_pct + "%" : "–"],
    ["Best call", tr.best ? `${tr.best.ticker} ${tr.best.fwd_return_pct >= 0 ? "+" : ""}${tr.best.fwd_return_pct}%` : "–"],
  ].map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  // Diary feed
  $("#ag-feed").innerHTML = events.length ? events.map(e =>
    `<div class="news-item"><span class="chip">${esc(e.kind)}</span>
     ${e.ticker ? `<b>${esc(e.ticker)}</b>` : ""}
     <span class="feed-note">${esc(e.note || "")}</span>
     <span class="news-meta">${esc(e.source)} · ${fmtAgo(e.ts)}</span></div>`).join("")
    : `<p class="muted">Nothing yet. Every Robinhood order, fill, and data pull the agent makes lands here the moment it happens.</p>`;

  // Report card table
  $("#ag-decisions tbody").innerHTML = decisions.length ? decisions.map(d => {
    const outcome = d.evaluated_at === null || d.evaluated_at === undefined
      ? '<span class="pill NEUTRAL">pending</span>'
      : d.hit === null
        ? `<span class="muted">${d.fwd_return_pct >= 0 ? "+" : ""}${d.fwd_return_pct}% (flat call)</span>`
        : `<span class="${d.hit ? "up" : "down"}">${d.fwd_return_pct >= 0 ? "+" : ""}${d.fwd_return_pct}% ${d.hit ? "✔ hit" : "✖ miss"}</span>`;
    return `<tr><td class="muted">${esc((d.ts || "").slice(0, 16).replace("T", " "))}</td>
      <td><b>${esc(d.ticker)}</b></td>
      <td><span class="pill ${ACTION_PILL[d.action] || "NEUTRAL"}">${esc(d.action)}</span></td>
      <td>${d.price !== null && d.price !== undefined ? fmt$(d.price) : "–"}</td>
      <td>${d.score !== null && d.score !== undefined ? (d.score >= 0 ? "+" : "") + d.score : "–"}</td>
      <td>${outcome}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="muted">No calls recorded yet — ask the agent to analyze a ticker.</td></tr>`;
}

/* ---------------- neighborhood (where the agents live) ---------------- */
/* One house per agent. The Trader's house renders its live state via the
   same agentState() the Agent tab uses; vacant lots are reserved for future
   agents — when one exists, give it a house here and a data source. Sky
   follows the viewer's local clock (override with ?hour=22 to preview). */

const MOOD_COLOR = () => ({ bullish: COLORS.good, bearish: COLORS.crit,
  neutral: COLORS.s1, asleep: "#8a8a80" });

/* The street is wider than the viewport; the SVG scales to fit, so adding a
   house means extending SCENE_W rather than cramming the existing lots. */
const SCENE_W = 1420;

const STARS = [[60, 40], [140, 88], [230, 30], [320, 66], [430, 24], [510, 90],
  [590, 44], [680, 20], [760, 74], [840, 36], [910, 96], [970, 50],
  [180, 130], [400, 120], [700, 118], [880, 140],
  [1040, 32], [1120, 84], [1210, 28], [1300, 70], [1060, 132], [1250, 124]];

/* Each junior agent's house reads its own last scan: awake if it ran within
   its stale window, porch light coloured by what it found, one line of live
   stats on the plaque. Returns nulls when the agent has never run. */
function residentState(res, staleHours, read) {
  const scan = res && res.last_scan;
  const ageH = scan ? (Date.now() - new Date(scan.ts).getTime()) / 3600000 : Infinity;
  const awake = ageH < staleHours;
  const payload = (scan && scan.payload) || null;
  const r = awake && payload ? read(payload) : null;
  return {
    awake,
    mood: !awake ? "asleep" : (r && r.mood) || "neutral",
    line: !awake ? "asleep" : (r && r.line) || "no findings",
    sent: (res && res.sent_today) || 0,
  };
}

async function loadTown() {
  try {
    renderTown(await api("/api/town/status"));
    $("#town-updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#town-scene").innerHTML = `<p class="err">${e.message}</p>`;
  }
}

function renderTown(t) {
  const s = t.trader.summary, decisions = t.trader.decisions || [];
  const st = agentState(s, decisions);
  const tr = s.track_record || {};
  const moodColor = MOOD_COLOR()[st.mood];
  const awake = st.mood !== "asleep";
  const mailWaiting = t.trader.unread_mail || 0;

  // The Analyst is awake if it scanned recently; its mood is the market's
  // news tone from that scan.
  const scan = t.analyst.last_scan;
  const scanAgeH = scan ? (Date.now() - new Date(scan.ts).getTime()) / 3600000 : Infinity;
  const aAwake = scanAgeH < 2;
  const tone = aAwake && scan.payload && scan.payload.overall
    ? scan.payload.overall.sentiment : null;
  const aMood = !aAwake ? "asleep"
    : tone === "bullish" ? "bullish" : tone === "bearish" ? "bearish" : "neutral";
  const aColor = MOOD_COLOR()[aMood];
  const sentToday = t.analyst.sent_today || 0;

  // The three junior agents. Stale windows are ~2× each agent's own cadence
  // (30 / 60 / 240 min), so one missed cycle doesn't put a house to sleep.
  // Blind is not calm. An agent with no broker snapshot reads the account
  // as unknown, and its house has to say so instead of showing a reassuring
  // "no positions" that looks identical to a genuinely flat book.
  const pm = residentState(t.position_manager, 1.5, (p) => {
    if (p.blind) return { mood: "bearish", line: "can't see account" };
    if (p.stale) return { mood: "bearish", line: `stale · ${Math.round(p.age_hours)}h old` };
    const n = p.n_positions || 0;
    if (!n) return { mood: "neutral", line: "no open positions" };
    if (p.unpriced) {
      return { mood: "bearish", line: `${n} held · ${p.unpriced} unpriced` };
    }
    const urgent = (p.earnings_warns || []).length
      || (p.actions || []).some((a) => a.type === "reversal");
    return {
      mood: urgent ? "bearish" : (p.actions || []).length ? "neutral" : "bullish",
      line: `${n} position${n === 1 ? "" : "s"} · ${(p.actions || []).length} to review`,
    };
  });
  const rm = residentState(t.risk_manager, 3, (p) => {
    if (p.blind) return { mood: "bearish", line: "can't see account" };
    if (p.stale) return { mood: "bearish", line: `stale · ${Math.round(p.age_hours)}h old` };
    // "unknown" means part of the book couldn't be priced — a warning, not
    // the calm green that anything-but-high/medium would otherwise give it.
    const mood = p.risk_level === "high" || p.risk_level === "unknown" ? "bearish"
      : p.risk_level === "medium" ? "neutral" : "bullish";
    return {
      mood,
      line: p.n_positions
        ? `Δ${p.net_delta >= 0 ? "+" : ""}${Math.round(p.net_delta)} · ${p.risk_level} risk`
        : "portfolio flat",
    };
  });
  const pe = residentState(t.pattern_engine, 12, (p) => {
    if (p.status !== "ok") {
      return { mood: "neutral", line: `${p.n_decisions || 0}/${p.min_required || 20} calls` };
    }
    const n = (p.findings || []).length;
    return {
      mood: n ? "bullish" : "neutral",
      line: `${n} edge${n === 1 ? "" : "s"} from ${p.n_decisions} calls`,
    };
  });

  const hourParam = new URLSearchParams(location.search).get("hour");
  const hour = hourParam !== null ? parseInt(hourParam, 10) : new Date().getHours();
  const night = !(hour >= 7 && hour < 19);

  const sky = night ? ["#0b1026", "#1c2547"] : ["#7db3dc", "#cfe8f6"];
  const grass = night ? "#1f3320" : "#3a5f38";
  const road = night ? "#20201f" : "#3a3a38";
  const walk = night ? "#3c3c38" : "#8d8d85";
  const wall = night ? "#8d8172" : "#cdbb9f";
  const roof = night ? "#4e332e" : "#7a4a42";
  const wallB = night ? "#67788c" : "#a5bacd";
  const roofB = night ? "#2f4256" : "#4a6a8a";
  const trim = night ? "#3b3f45" : "#575d66";
  const winLit = "#ffd98a", winDark = night ? "#141b26" : "#2a3442";

  const celestial = night
    ? `<circle cx="1268" cy="54" r="30" fill="#e8e6d8"/>
       <circle cx="1258" cy="46" r="6" fill="#cdcaba"/><circle cx="1280" cy="64" r="4" fill="#cdcaba"/>
       ${STARS.map(([x, y]) => `<circle cx="${x}" cy="${y}" r="1.6" fill="#e9edf5" class="star"/>`).join("")}`
    : `<circle cx="1268" cy="54" r="34" fill="#ffd76a"/>
       <circle cx="1268" cy="54" r="46" fill="#ffd76a" opacity="0.25"/>`;

  const lamp = (x) => `
    <g>
      <rect x="${x}" y="230" width="6" height="120" fill="${trim}"/>
      <rect x="${x - 10}" y="222" width="26" height="10" rx="5" fill="${trim}"/>
      <circle cx="${x + 3}" cy="238" r="7" fill="#ffd98a" opacity="${night ? 0.95 : 0.15}" class="lamp"/>
      ${night ? `<circle cx="${x + 3}" cy="238" r="16" fill="#ffd98a" opacity="0.18"/>` : ""}
    </g>`;

  const win = (x, y, lit, w = 54, h = 46) => `
    <g>
      <rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${lit ? winLit : winDark}"
        stroke="${trim}" stroke-width="3" class="${lit ? "win-lit" : ""}"/>
      <line x1="${x + w / 2}" y1="${y}" x2="${x + w / 2}" y2="${y + h}" stroke="${trim}" stroke-width="2"/>
      <line x1="${x}" y1="${y + h / 2}" x2="${x + w}" y2="${y + h / 2}" stroke="${trim}" stroke-width="2"/>
    </g>`;

  const traderHouse = (x) => `
    <g class="house house-trader" role="button" tabindex="0">
      <title>The Trader — click to step inside</title>
      <rect x="${x + 176}" y="66" width="24" height="60" fill="#6b4a40"/>
      ${awake ? `<circle cx="${x + 188}" cy="52" r="7" class="smoke s1"/>
                 <circle cx="${x + 188}" cy="40" r="9" class="smoke s2"/>
                 <circle cx="${x + 188}" cy="26" r="11" class="smoke s3"/>` : ""}
      <polygon points="${x - 14},132 ${x + 122},52 ${x + 258},132" fill="${roof}"/>
      <rect x="${x}" y="132" width="244" height="172" fill="${wall}"/>
      <rect x="${x + 100}" y="224" width="46" height="80" fill="#57422f"/>
      <circle cx="${x + 138}" cy="266" r="4" fill="#c9a86a"/>
      <circle cx="${x + 123}" cy="206" r="8" fill="${moodColor}" class="porch"/>
      ${win(x + 24, 160, awake)}
      ${win(x + 168, 160, awake)}
      ${awake
        ? `<circle cx="${x + 51}" cy="186" r="13" fill="#cfd6d2"/>
           <circle cx="${x + 45}" cy="184" r="3" fill="${moodColor}"/>
           <circle cx="${x + 57}" cy="184" r="3" fill="${moodColor}"/>`
        : `<text x="${x + 200}" y="150" class="town-zzz">z</text>
           <text x="${x + 216}" y="136" class="town-zzz z2">z</text>`}
      <rect x="${x - 44}" y="252" width="6" height="52" fill="${trim}"/>
      <rect x="${x - 62}" y="238" width="44" height="26" rx="5" fill="${trim}"/>
      ${mailWaiting ? `<polygon points="${x - 62},242 ${x - 62},224 ${x - 50},233" fill="${COLORS.crit}"/>
        <text x="${x - 40}" y="256" font-size="15" font-weight="700" fill="#ffd98a"
          text-anchor="middle">${mailWaiting > 99 ? "99" : mailWaiting}</text>` : ""}
      <rect x="${x + 74}" y="312" width="96" height="30" rx="5" fill="${trim}"/>
      <text x="${x + 122}" y="326" class="town-label" text-anchor="middle">THE TRADER · №1</text>
      <text x="${x + 122}" y="338" class="town-sub" text-anchor="middle">
        ${tr.hit_rate !== null && tr.hit_rate !== undefined ? tr.hit_rate + "% career" : "unproven"} ·
        ${st.mood}</text>
    </g>`;

  const analystHouse = (x) => `
    <g class="house house-analyst" role="button" tabindex="0">
      <title>The Analyst — reads the news so the Trader doesn't have to. Click for headlines.</title>
      <line x1="${x + 122}" y1="58" x2="${x + 122}" y2="26" stroke="${trim}" stroke-width="4" stroke-linecap="round"/>
      <line x1="${x + 104}" y1="34" x2="${x + 140}" y2="34" stroke="${trim}" stroke-width="3" stroke-linecap="round"/>
      <circle cx="${x + 122}" cy="22" r="5" fill="${aColor}" class="porch"/>
      <polygon points="${x - 14},132 ${x + 122},52 ${x + 258},132" fill="${roofB}"/>
      <rect x="${x}" y="132" width="244" height="172" fill="${wallB}"/>
      <rect x="${x + 100}" y="224" width="46" height="80" fill="#3f4c5c"/>
      <circle cx="${x + 138}" cy="266" r="4" fill="#c9d4e0"/>
      <circle cx="${x + 123}" cy="206" r="8" fill="${aColor}" class="porch"/>
      ${win(x + 24, 160, aAwake)}
      ${win(x + 168, 160, aAwake)}
      ${aAwake
        ? `<circle cx="${x + 195}" cy="186" r="13" fill="#cfd6d2"/>
           <circle cx="${x + 189}" cy="184" r="3" fill="${aColor}"/>
           <circle cx="${x + 201}" cy="184" r="3" fill="${aColor}"/>
           <rect x="${x + 182}" y="192" width="26" height="9" fill="#f0ead6" stroke="${trim}" stroke-width="1"/>
           <line x1="${x + 186}" y1="196" x2="${x + 204}" y2="196" stroke="#8a8a80" stroke-width="1"/>`
        : `<text x="${x + 56}" y="150" class="town-zzz">z</text>
           <text x="${x + 72}" y="136" class="town-zzz z2">z</text>`}
      <rect x="${x + 58}" y="292" width="20" height="7" fill="#f0ead6" stroke="${trim}" stroke-width="1"/>
      <rect x="${x + 62}" y="285" width="20" height="7" fill="#e6dfc8" stroke="${trim}" stroke-width="1"/>
      <rect x="${x + 288}" y="252" width="6" height="52" fill="${trim}"/>
      <rect x="${x + 282}" y="238" width="44" height="26" rx="5" fill="${trim}"/>
      ${sentToday ? `<polygon points="${x + 326},242 ${x + 326},224 ${x + 314},233" fill="${COLORS.good}"/>
        <text x="${x + 304}" y="256" font-size="15" font-weight="700" fill="#ffd98a"
          text-anchor="middle">${sentToday > 99 ? "99" : sentToday}</text>` : ""}
      <rect x="${x + 70}" y="312" width="104" height="30" rx="5" fill="${trim}"/>
      <text x="${x + 122}" y="326" class="town-label" text-anchor="middle">THE ANALYST · №2</text>
      <text x="${x + 122}" y="338" class="town-sub" text-anchor="middle">
        ${aAwake ? `${(scan.payload && scan.payload.n_headlines) || "?"} headlines ·
          ${aMood === "neutral" ? "mixed" : aMood} tone` : "asleep"}</text>
    </g>`;

  /* The junior agents' cottages: same street, half the footprint. 140 wide,
     base on the grass line at y=304 exactly like the two big houses, so the
     row reads as one block. `roof` distinguishes the trade (see callers). */
  const cottage = (x, cls, num, name, title, state, tint, rooftop) => `
    <g class="house ${cls}" role="button" tabindex="0">
      <title>${title}</title>
      ${rooftop(x)}
      <polygon points="${x - 10},188 ${x + 70},134 ${x + 150},188" fill="${tint.roof}"/>
      <rect x="${x}" y="188" width="140" height="116" fill="${tint.wall}"/>
      ${win(x + 16, 208, state.awake, 38, 34)}
      ${win(x + 86, 208, state.awake, 38, 34)}
      ${state.awake ? "" : `<text x="${x + 112}" y="204" class="town-zzz">z</text>
        <text x="${x + 126}" y="192" class="town-zzz z2">z</text>`}
      <rect x="${x + 55}" y="256" width="30" height="48" fill="#57422f"/>
      <circle cx="${x + 78}" cy="281" r="3" fill="#c9a86a"/>
      <circle cx="${x + 70}" cy="248" r="6" fill="${MOOD_COLOR()[state.mood]}" class="porch"/>
      <rect x="${x + 158}" y="262" width="5" height="42" fill="${trim}"/>
      <rect x="${x + 152}" y="250" width="34" height="20" rx="4" fill="${trim}"/>
      ${state.sent ? `<polygon points="${x + 186},253 ${x + 186},238 ${x + 176},246" fill="${COLORS.good}"/>
        <text x="${x + 169}" y="265" font-size="12" font-weight="700" fill="#ffd98a"
          text-anchor="middle">${state.sent > 99 ? "99" : state.sent}</text>` : ""}
      <rect x="${x + 6}" y="312" width="128" height="30" rx="5" fill="${trim}"/>
      <text x="${x + 70}" y="326" class="town-label" text-anchor="middle">${name} · №${num}</text>
      <text x="${x + 70}" y="338" class="town-sub" text-anchor="middle">${esc(state.line)}</text>
    </g>`;

  // Position Manager: a watchtower cupola — it stands over the open book.
  const pmRoof = (x) => `
    <rect x="${x + 58}" y="108" width="24" height="30" fill="${trim}"/>
    <polygon points="${x + 52},108 ${x + 70},92 ${x + 88},108" fill="${roof}"/>
    <circle cx="${x + 70}" cy="121" r="5" fill="${pm.awake ? winLit : winDark}" class="${pm.awake ? "win-lit" : ""}"/>`;

  // Risk Manager: scales on the ridge — the portfolio weighed against itself.
  const rmRoof = (x) => `
    <line x1="${x + 70}" y1="134" x2="${x + 70}" y2="104" stroke="${trim}" stroke-width="4" stroke-linecap="round"/>
    <line x1="${x + 44}" y1="106" x2="${x + 96}" y2="106" stroke="${trim}" stroke-width="3" stroke-linecap="round"/>
    <path d="M${x + 44} 106 l-7 12 h14 z" fill="${MOOD_COLOR()[rm.mood]}"/>
    <path d="M${x + 96} 106 l-7 12 h14 z" fill="${MOOD_COLOR()[rm.mood]}"/>`;

  // Pattern Engine: a memory spire, brighter the more edges it has found.
  const peRoof = (x) => `
    <line x1="${x + 70}" y1="134" x2="${x + 70}" y2="98" stroke="${trim}" stroke-width="4" stroke-linecap="round"/>
    ${[0, 1, 2].map((i) => `<circle cx="${x + 70}" cy="${112 + i * 11}" r="${7 - i * 1.5}"
      fill="${MOOD_COLOR()[pe.mood]}" opacity="${pe.awake ? 0.9 - i * 0.22 : 0.25}"/>`).join("")}`;

  const vacantLot = (x, num) => `
    <g class="lot">
      <title>Reserved for your next agent</title>
      ${[0, 1, 2, 3, 4, 5, 6, 7].map(i =>
        `<rect x="${x + i * 26}" y="256" width="10" height="42" rx="2" fill="${trim}" opacity="0.75"/>`).join("")}
      <rect x="${x - 4}" y="266" width="216" height="6" fill="${trim}" opacity="0.75"/>
      <circle cx="${x + 34}" cy="296" r="15" fill="${grass}" stroke="${night ? "#2c452c" : "#4d7a49"}" stroke-width="3"/>
      <circle cx="${x + 180}" cy="298" r="12" fill="${grass}" stroke="${night ? "#2c452c" : "#4d7a49"}" stroke-width="3"/>
      <rect x="${x + 86}" y="206" width="8" height="94" fill="#6d5a41"/>
      <rect x="${x + 28}" y="182" width="156" height="46" rx="6" fill="#e8dcc2" stroke="#6d5a41" stroke-width="3"/>
      <text x="${x + 106}" y="202" font-size="15" font-weight="800" fill="#5d4633" text-anchor="middle">RESERVED</text>
      <text x="${x + 106}" y="219" font-size="11" fill="#7a6a50" text-anchor="middle">for agent №${num}</text>
    </g>`;

  const stripes = [];
  for (let x = 40; x < SCENE_W; x += 140) stripes.push(x);

  $("#town-scene").innerHTML = `
    <svg viewBox="0 0 ${SCENE_W} 400" role="img" aria-label="Agent neighborhood"
      style="width:100%;height:auto;display:block;border-radius:8px">
      <defs>
        <linearGradient id="skyG" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="${sky[0]}"/><stop offset="1" stop-color="${sky[1]}"/>
        </linearGradient>
      </defs>
      <rect width="${SCENE_W}" height="316" fill="url(#skyG)"/>
      ${celestial}
      <rect y="300" width="${SCENE_W}" height="44" fill="${grass}"/>
      <rect y="344" width="${SCENE_W}" height="14" fill="${walk}"/>
      <rect y="358" width="${SCENE_W}" height="42" fill="${road}"/>
      ${stripes.map(x =>
        `<rect x="${x}" y="377" width="56" height="5" rx="2.5" fill="#c9c245" opacity="0.8"/>`).join("")}
      ${lamp(348)} ${lamp(742)} ${lamp(1370)}
      ${traderHouse(80)}
      ${analystHouse(400)}
      ${cottage(770, "house-pm", 3, "POSITIONS", "The Position Manager — watches open positions for exits, rolls, expiry and earnings risk. Click for your positions.", pm, { roof, wall: night ? "#8c7c86" : "#c9b3bf" }, pmRoof)}
      ${cottage(960, "house-rm", 4, "RISK", "The Risk Manager — weighs portfolio delta, theta and concentration. Click for your positions.", rm, { roof: roofB, wall: wallB }, rmRoof)}
      ${cottage(1150, "house-pe", 5, "PATTERNS", "The Pattern Engine — mines graded calls for repeatable edge. Click for the track record.", pe, { roof: night ? "#33452f" : "#4a6a42", wall: night ? "#7d8a72" : "#b7c6a6" }, peRoof)}
    </svg>`;

  const wire = (sel, tab) => {
    const h = $("#town-scene " + sel);
    if (!h) return;
    h.style.cursor = "pointer";
    h.addEventListener("click", () => switchTab(tab));
    h.addEventListener("keydown", (e) => { if (e.key === "Enter") switchTab(tab); });
  };
  wire(".house-trader", "agent");
  wire(".house-analyst", "news");
  wire(".house-pm", "journal");
  wire(".house-rm", "journal");
  wire(".house-pe", "agent");
}

/* ---------------- daily trend ---------------- */

const trendPct = (v) =>
  v === null || v === undefined ? '<span class="muted">–</span>'
    : `<span class="${v >= 0 ? "up" : "down"}">${v >= 0 ? "+" : ""}${v}%</span>`;

async function loadTrend() {
  try {
    const d = await api("/api/trend");
    const s = d.summary;

    // Say which universe this is. On the public deployment there is no
    // tracking store, so it falls back to the generic watchlist — and a table
    // headed "the stocks you trade" had better admit when it is not those.
    const scope = d.source === "account"
      ? `${s.n} ticker(s) you hold or have traded`
      : `${s.n} ticker(s) from the default watchlist — no account history on this instance`;
    $("#trend-summary").innerHTML =
      `<b>${s.tone}</b> — ${s.up} rising, ${s.down} falling, ${s.sideways} sideways. ${scope}.`;
    $("#trend-updated").textContent = d.generated_at ? `updated ${d.generated_at} UTC` : "";

    $("#trend-table tbody").innerHTML = d.trends.map(t => {
      if (t.error) {
        return `<tr><td><b>${t.ticker}</b></td><td colspan="10" class="err">${t.error}</td></tr>`;
      }
      const arrow = t.direction === "up" ? "▲" : t.direction === "down" ? "▼" : "▬";
      const cls = t.direction === "up" ? "up" : t.direction === "down" ? "down" : "muted";
      // A conflicted read is the one number a trader must not skim past, so it
      // is labelled in place rather than left to be inferred from the columns.
      const dir = t.conflict
        ? `<span class="muted" title="The 20-day fit and the net move disagree — treat direction as unresolved">▬ mixed signal</span>`
        : `<span class="${cls}">${arrow} ${t.direction}</span>`;
      const streak = t.streak === 0 ? '<span class="muted">–</span>'
        : `<span class="${t.streak > 0 ? "up" : "down"}">${Math.abs(t.streak)}d ${t.streak > 0 ? "up" : "down"}</span>`;
      const stack = t.ma_stack
        ? `<span class="${t.ma_stack === "bullish" ? "up" : "down"}">${t.above_sma20 ? "above" : "below"} 20 · ${t.ma_stack}</span>`
        : '<span class="muted">–</span>';
      const held = t.origin === "held" ? '<span class="up">✔ held</span>'
        : t.origin === "decided" ? '<span class="muted">traded</span>'
        : '<span class="muted">–</span>';
      return `<tr class="scan-row" data-t="${t.ticker}">
        <td><b>${t.ticker}</b></td><td>${fmt$(t.price)}</td>
        <td>${dir}</td><td class="muted">${t.strength}</td><td>${streak}</td>
        <td>${trendPct(t.change_1d)}</td><td>${trendPct(t.change_5d)}</td><td>${trendPct(t.change_20d)}</td>
        <td class="muted">${t.consistency === null ? "–" : t.consistency + "%"}</td>
        <td>${stack}</td><td>${held}</td></tr>`;
    }).join("");

    $("#trend-note").textContent =
      `Trend is the ${d.lookback_days}-day regression slope measured in ATR per day, so a $500 stock `
      + `and a $15 stock are compared on the same scale. "One-way" is the share of days that moved with `
      + `the trend — same slope with 75% is a cleaner move than with 45%. Rows marked "mixed signal" are `
      + `ones where the slope and the net 20-day move disagree; direction there is genuinely unresolved, not up or down.`;

    document.querySelectorAll("#trend-table .scan-row").forEach(r => {
      r.addEventListener("click", () => {
        currentTicker = r.dataset.t;
        $("#ticker-input").value = currentTicker;
        switchTab("analyze");
      });
    });
  } catch (e) {
    $("#trend-summary").innerHTML = `<span class="err">Could not load trend: ${e.message}</span>`;
  }
}

/* ---------------- scanner + calendar + alerts ---------------- */

async function loadScanner() {
  try {
    const d = await api("/api/scan");
    $("#scan-table tbody").innerHTML = d.setups.map((s, i) => s.error
      ? `<tr><td>${i + 1}</td><td>${s.ticker}</td><td colspan="8" class="err">${s.error}</td></tr>`
      : `<tr class="scan-row" data-t="${s.ticker}">
        <td>${i + 1}</td><td><b>${s.ticker}</b></td><td>${fmt$(s.close)}</td>
        <td class="${s.score >= 0 ? "up" : "down"}">${s.score >= 0 ? "+" : ""}${s.score}</td>
        <td><span class="pill ${pillClass(s.label)}">${s.label}</span></td>
        <td class="muted">${s.weekly_label}</td>
        <td>${s.confluence === "agree" ? '<span class="up">✔ agree</span>'
            : s.confluence === "conflict" ? '<span class="down">✖ conflict</span>' : '<span class="muted">mixed</span>'}</td>
        <td>${s.crossed_buy ? '<span class="pill BUY">BUY cross</span>'
            : s.crossed_sell ? '<span class="pill SELL">SELL cross</span>' : ""}</td>
        <td class="${s.earnings_in_days !== null && s.earnings_in_days <= 7 ? "warn-text" : "muted"}">${s.earnings_in_days ?? "–"}${s.earnings_in_days !== null ? "d" : ""}</td>
        <td>${s.rank}</td></tr>`).join("");
    document.querySelectorAll(".scan-row").forEach(r => r.addEventListener("click", () => {
      currentTicker = r.dataset.t; $("#ticker-input").value = currentTicker; switchTab("analyze");
    }));
    $("#scan-note").textContent = d.note;
    $("#scan-updated").textContent = "generated " + d.generated_at;
  } catch (e) {
    $("#scan-table tbody").innerHTML = `<tr><td colspan="10" class="err">${e.message}</td></tr>`;
  }
  try {
    const c = await api("/api/calendar");
    $("#cal-table tbody").innerHTML = c.events.length ? c.events.map(ev =>
      `<tr><td>${ev.date}</td><td class="${ev.in_days <= 3 ? "warn-text" : ""}">${ev.in_days}d</td>
       <td><b>${ev.kind}</b></td><td class="muted">${ev.why}</td></tr>`).join("")
      : `<tr><td colspan="4" class="muted">Nothing scheduled in this window.</td></tr>`;
  } catch (e) { /* calendar is best-effort */ }
  renderAlerts();
}

let alertsCache = [];

async function pollAlerts() {
  try {
    const seen = parseInt(localStorage.getItem("lastAlertSeen") || "0", 10);
    const d = await api("/api/alerts?since_id=0");
    alertsCache = d.alerts;
    const unseen = alertsCache.filter(a => a.id > seen);
    $("#bell-count").textContent = unseen.length;
    $("#bell-count").hidden = unseen.length === 0;
    const notified = parseInt(localStorage.getItem("lastAlertNotified") || "0", 10);
    const fresh = alertsCache.filter(a => a.id > notified);
    if (fresh.length && "Notification" in window && Notification.permission === "granted") {
      fresh.slice(0, 3).forEach(a =>
        new Notification("⚡ " + (a.ticker || "Market"), { body: a.message, icon: "/static/icon-192.png" }));
      localStorage.setItem("lastAlertNotified", String(Math.max(...fresh.map(a => a.id))));
    }
    if (activeTab() === "scanner") renderAlerts();
  } catch (e) { /* poll again later */ }
}

function renderAlerts() {
  if (!alertsCache.length) return;
  $("#alerts-list").innerHTML = alertsCache.slice(0, 30).map(a =>
    `<div class="news-item"><span class="dot ${a.kind.includes("sell") || a.kind.includes("position") ? "dot-neg" : "dot-pos"}"></span>
     <span>${a.message}</span><span class="news-meta">${a.created_at} UTC</span></div>`).join("");
}

$("#bell").addEventListener("click", () => {
  if (alertsCache.length) localStorage.setItem("lastAlertSeen", String(Math.max(...alertsCache.map(a => a.id))));
  $("#bell-count").hidden = true;
  switchTab("scanner");
});

$("#alerts-notify-btn").addEventListener("click", async () => {
  if (!("Notification" in window)) return alert("This browser doesn't support notifications.");
  const p = await Notification.requestPermission();
  $("#alerts-notify-btn").textContent = p === "granted" ? "✔ Notifications on" : "Notifications blocked";
});

/* /* ---------------- analyze ---------------- */

function renderAnalysisView(d) {
  const s = d.signal;
  const isBeg = userLevel === "beginner";
  const modeNote = isBeg
    ? `<div class="coach-card"><div class="coach-head"><span class="coach-badge">🌱 Beginner Chart Guide</span><button type="button" class="explain-btn" onclick="openExplainer('signal_score', {ticker:'${currentTicker}', val:${s.score}, label:'${s.label}'})">ⓘ How This Score Works</button></div><div class="coach-body"><b>${currentTicker}</b> is at <b>${fmt$(s.close)}</b> with a technical score of <b>${s.score >= 0 ? "+" : ""}${s.score} (${s.label})</b>. The blue line is the stock price; the orange (20-day) and green (50-day) lines show the short- and medium-term trend. When price stays above both lines, the trend is healthy.</div></div>`
    : userLevel === "quant"
    ? `<div class="coach-card"><div class="coach-head"><span class="coach-badge">🔬 Quant Technical Diagnostics</span></div><div class="coach-body">Close $S_t = ${fmt$(s.close)}$ · $\\text{RSI}_{14} = ${s.rsi ?? "–"}$ · $\\text{ATR}_{14} = ${fmt$(s.atr)}$ (${s.close && s.atr ? ((s.atr / s.close) * 100).toFixed(2) + "% daily vol" : "–"}). Tap any component row in the table below to inspect its factor math.</div></div>`
    : "";

  $("#an-signal").innerHTML = `
    ${modeNote}
    <span class="score-big ${s.score >= 0 ? "up" : "down"}" data-explain="signal_score" data-ctx="${esc(JSON.stringify({ticker: currentTicker, val: s.score, label: s.label}))}" style="cursor:pointer">${s.score >= 0 ? "+" : ""}${s.score}</span>
    <span class="pill ${pillClass(s.label)}">${s.label}</span>
    <span class="muted">close ${fmt$(s.close)} · RSI ${s.rsi ?? "–"} · ATR ${s.atr ?? "–"}</span>`;
  bindExplainableElements($("#an-signal"));

  const ch = d.chart;
  drawChart("chart-price", ch.dates, [
    { name: "Close", data: ch.close, color: COLORS.s1, width: 2 },
    { name: "SMA20", data: ch.sma20, color: COLORS.s2, width: 1.5 },
    { name: "SMA50", data: ch.sma50, color: COLORS.s3, width: 1.5 },
    { name: "BB up", data: ch.bb_upper, color: COLORS.muted, width: 1, dash: [3, 3] },
    { name: "BB low", data: ch.bb_lower, color: COLORS.muted, width: 1, dash: [3, 3] },
  ]);
  drawChart("chart-rsi", ch.dates, [{ name: "RSI", data: ch.rsi, color: COLORS.s1 }],
    { min: 0, max: 100, refLines: [{ value: 30, color: COLORS.good }, { value: 70, color: COLORS.crit }] });
  drawChart("chart-score", ch.dates, [{ name: "Score", data: ch.score, color: COLORS.s2 }],
    { min: -100, max: 100, refLines: [{ value: 20, color: COLORS.good }, { value: -15, color: COLORS.crit }, { value: 0, color: COLORS.baseline }] });
  const tbody = $("#votes-table tbody");
  tbody.innerHTML = s.votes.map(v =>
    `<tr data-explain="signal_score" data-ctx="${esc(JSON.stringify({ticker: currentTicker, val: s.score, label: s.label}))}"><td><b>${v.component}</b></td><td>${v.weight}</td>
     <td class="${v.score >= 0 ? "up" : "down"}">${v.score >= 0 ? "+" : ""}${v.score}</td>
     <td class="muted">${v.reason} <span style="color:var(--series-1);font-size:11px">ⓘ</span></td></tr>`).join("");
  bindExplainableElements(tbody);
}

async function loadAnalysis() {
  $("#an-ticker").textContent = currentTicker;
  const [period, interval] = $("#an-period").value.split("|");
  try {
    const d = await api(`/api/analysis/${currentTicker}?period=${period}&interval=${interval}`);
    lastAnalysisData = d;
    renderAnalysisView(d);
    $("#an-updated").textContent = "updated " + new Date().toLocaleTimeString();
    loadScorecard();
  } catch (e) {
    $("#an-signal").innerHTML = `<span class="err">${e.message}</span>`;
  }
}
$("#an-period").addEventListener("change", loadAnalysis);

async function loadScorecard() {
  const box = $("#scorecard-box");
  box.innerHTML = `<p class="spin">Grading 3 years of this signal's own calls…</p>`;
  try {
    const d = await api(`/api/scorecard/${currentTicker}`);
    const row = (name, s) => s.count
      ? `<div class="tile" data-explain="win_rate" data-ctx="${esc(JSON.stringify({ticker: currentTicker, val: s.win_rate + '%'}))}"><div class="k">${name} (${s.count} signals)</div>
         <div class="v ${s.win_rate >= 55 ? "up" : s.win_rate <= 45 ? "down" : ""}">${s.win_rate}% win</div>
         <div class="muted">avg ${s.avg_fwd_return_pct >= 0 ? "+" : ""}${s.avg_fwd_return_pct}% over ${d.horizon_days} bars</div></div>`
      : `<div class="tile"><div class="k">${name}</div><div class="v muted">no signals</div></div>`;
    box.innerHTML = `<div class="tiles">${row("BUY crossings", d.buy_signals)}${row("SELL crossings", d.sell_signals)}</div>
      ${d.buy_signals.recent.length ? `<table><thead><tr><th>Date</th><th>Score</th><th>Fwd return</th><th>Result</th></tr></thead>
      <tbody>${d.buy_signals.recent.map(e => `<tr><td>${e.date}</td><td>+${e.score}</td>
        <td class="${e.fwd_return_pct >= 0 ? "up" : "down"}">${e.fwd_return_pct >= 0 ? "+" : ""}${e.fwd_return_pct}%</td>
        <td>${e.win ? '<span class="up">✔ win</span>' : '<span class="down">✖ loss</span>'}</td></tr>`).join("")}</tbody></table>` : ""}
      <p class="muted">${d.note}</p>`;
    bindExplainableElements(box);
  } catch (e) {
    box.innerHTML = `<p class="err">${e.message}</p>`;
  }
}

/* ---------------- options ---------------- */

function renderOptionsView(d) {
  const rec = d.recommendation;
  const isBeg = userLevel === "beginner";
  const lbl = (beg, std) => isBeg ? beg : std;
  const sug = rec.suggestion;
  const contractCost = sug?.est_mid_price ? Math.round(sug.est_mid_price * 100) : null;

  const begBanner = isBeg
    ? `<div class="coach-card"><div class="coach-head"><span class="coach-badge">🌱 Beginner Options Translator</span><button type="button" class="explain-btn" onclick="openExplainer('dte', {ticker:'${currentTicker}', val:${d.dte}})">ⓘ Why DTE &amp; Time Decay Matter</button></div><div class="coach-body">${sug ? `1 contract controls <b>100 shares</b>. Buying the <b>$${sug.strike} option (${d.dte} days left)</b> at <b>${fmt$(sug.est_mid_price)}</b> costs about <b>$${contractCost} out of pocket</b>. That $${contractCost} is the most you can lose. Because this expiration has <b>${d.dte} days</b> left, daily time decay is ${fmt$(Math.abs(sug.theta || 0) * 100)}/day per contract.` : `Currently rated <b>${rec.bias}</b>. Tap any metric tile below to see what it means in plain English.`}</div></div>`
    : "";

  $("#op-rec").innerHTML = `
    ${begBanner}
    <div class="signal-row">
      <span class="pill ${pillClass(rec.bias)}">${rec.bias}</span>
      <span class="score-big ${rec.combined_score >= 0 ? "up" : "down"}">${rec.combined_score >= 0 ? "+" : ""}${rec.combined_score}</span>
      <span class="muted">combined stock + options score</span>
    </div>
    ${(rec.warnings || []).map(w => `<div class="warn-box">⚠ ${w}</div>`).join("")}
    ${rec.suggestion ? `<p><b>${rec.suggestion.action}</b> $${rec.suggestion.strike} exp ${rec.suggestion.expiry}
      — Δ ${rec.suggestion.delta}, Θ ${rec.suggestion.theta}/day, IV ${rec.suggestion.iv}%, mid ≈ ${fmt$(rec.suggestion.est_mid_price)} (${contractCost ? "~$" + contractCost + "/contract" : ""})<br>
      <span class="muted">${rec.suggestion.note}</span></p>` : ""}
    ${rec.spread ? `<p><b>Defined-risk alternative — ${rec.spread.name}:</b>
      buy $${rec.spread.buy_strike} / sell $${rec.spread.sell_strike} for ≈ ${fmt$(rec.spread.net_debit)} debit.
      Max loss ${fmt$(rec.spread.max_loss)}, max profit ${fmt$(rec.spread.max_profit)}
      (${rec.spread.reward_risk}:1), breakeven ${rec.spread.breakeven}.<br>
      <span class="muted">${rec.spread.note}</span></p>` : ""}
    ${d.iv_context ? `<p class="muted">IV context: ${d.iv_context.note}</p>` : ""}
    <ul class="reasons">${rec.reasons.map(r => `<li>${r}</li>`).join("")}</ul>`;

  const metricsSpec = [
    [lbl("Stock Price", "Spot"), fmt$(d.spot), "signal_score", d.spot],
    [lbl("Days Left (DTE)", "DTE"), d.dte, "dte", d.dte],
    [lbl("Earnings Date", "Next earnings"), d.earnings?.next_earnings ? `${d.earnings.next_earnings}${d.earnings_before_expiry ? " ⚠" : ""}` : "–", "earnings", d.earnings?.next_earnings],
    [lbl("Option Price Level (IV)", "ATM IV"), (d.atm_iv ?? "–") + "%", "atm_iv", d.atm_iv],
    [lbl("Market's ± Range", "Expected move"), "±" + (d.expected_move ?? "–") + ` (${d.expected_move_pct ?? "–"}%)`, "expected_move", "±$" + (d.expected_move ?? "–")],
    [lbl("Crowd Fear/Greed (Vol)", "P/C ratio (vol)"), d.put_call_ratio_volume ?? "–", "put_call_ratio", d.put_call_ratio_volume],
    [lbl("Open Positions P/C", "P/C ratio (OI)"), d.put_call_ratio_oi ?? "–", "put_call_ratio", d.put_call_ratio_oi],
    [lbl("Downside Fear Skew", "IV skew"), (d.iv_skew ?? "–") + " pts", "atm_iv", d.atm_iv],
    [lbl("Cheap vs Expensive", "IV context"), d.iv_context ? `${d.iv_context.label} (${d.iv_context.percentile}%)` : "–", "atm_iv", d.atm_iv],
    [lbl("Option Pin Price", "Max pain"), d.max_pain ?? "–", "max_pain", d.max_pain],
  ];
  $("#op-metrics").innerHTML = metricsSpec.map(([k, v, topic, rawVal]) =>
    `<div class="tile" data-explain="${topic}" data-ctx="${esc(JSON.stringify({ticker: currentTicker, val: rawVal}))}"><div class="k">${k}</div><div class="v">${v}</div></div>`
  ).join("");
  bindExplainableElements($("#op-metrics"));

  const row = (r) => {
    const atm = Math.abs(r.strike - d.spot) / d.spot < 0.005;
    return `<tr class="${r.in_the_money ? "itm" : ""} ${atm ? "atm-row" : ""}">
      <td><b>${r.strike}</b></td><td>${r.bid ?? "–"}</td><td>${r.ask ?? "–"}</td><td>${r.last ?? "–"}</td>
      <td>${r.volume ?? 0}</td><td>${r.open_interest ?? 0}</td><td>${r.iv ?? "–"}</td>
      <td>${r.delta ?? "–"}</td><td>${r.theta ?? "–"}</td></tr>`;
  };
  $("#calls-table tbody").innerHTML = d.calls.map(row).join("");
  $("#puts-table tbody").innerHTML = d.puts.map(row).join("");
  $("#unusual-table tbody").innerHTML = d.unusual_activity.length
    ? d.unusual_activity.map(u => `<tr><td><span class="pill ${u.type === "call" ? "CALL" : "PUT"}">${u.type.toUpperCase()}</span></td>
        <td>${u.strike}</td><td>${u.volume}</td><td>${u.open_interest}</td><td>${u.last ?? "–"}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">None detected at this expiry.</td></tr>`;
}

async function loadOptions(expiry) {
  $("#op-ticker").textContent = currentTicker;
  $("#op-rec").innerHTML = `<p class="spin">Loading options chain…</p>`;
  try {
    const d = await api(`/api/options/${currentTicker}` + (expiry ? `?expiry=${expiry}` : ""));
    lastOptionsData = d;
    const sel = $("#op-expiry");
    sel.innerHTML = d.expiries.map(e => `<option ${e === d.expiry ? "selected" : ""}>${e}</option>`).join("");
    sel.onchange = () => loadOptions(sel.value);
    renderOptionsView(d);
    $("#op-updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#op-rec").innerHTML = `<p class="err">Options load failed: ${e.message}</p>`;
  }
}

/* ---------------- backtest ---------------- */

$("#bt-run").addEventListener("click", runBacktest);

async function runBacktest() {
  $("#bt-ticker").textContent = currentTicker;
  $("#bt-stats").innerHTML = `<div class="spin">Running backtest…</div>`;
  const qs = new URLSearchParams({
    period: $("#bt-period").value,
    buy_threshold: $("#bt-buy").value,
    sell_threshold: $("#bt-sell").value,
    stop_atr: $("#bt-stop").value,
    target_atr: $("#bt-target").value,
    max_hold: $("#bt-hold").value,
    direction: $("#bt-dir").value,
  });
  try {
    const d = await api(`/api/backtest/${currentTicker}?` + qs);
    $("#bt-stats").innerHTML = [
      ["Trades", d.num_trades],
      ["Win rate", (d.win_rate ?? "–") + "%"],
      ["Profit factor", d.profit_factor ?? "–"],
      ["Expectancy / trade", (d.expectancy_pct ?? "–") + "%"],
      ["Avg win", (d.avg_win_pct ?? "–") + "%"],
      ["Avg loss", (d.avg_loss_pct ?? "–") + "%"],
      ["Strategy return", d.total_return_pct + "%"],
      ["Option-proxy return", d.option_proxy_return_pct + "%"],
      ["Buy & hold", d.buy_hold_return_pct + "%"],
      ["Max drawdown", d.max_drawdown_pct + "%"],
    ].map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    drawChart("chart-equity", d.equity_curve.map((_, i) => "trade " + i),
      [{ name: "Equity", data: d.equity_curve, color: COLORS.s1 }],
      { refLines: [{ value: 1, color: COLORS.baseline }] });
    $("#bt-trades tbody").innerHTML = d.trades.slice().reverse().map(t =>
      `<tr><td>${t.entry_date}</td><td>${t.exit_date}</td><td>${t.entry}</td><td>${t.exit}</td>
       <td class="${t.return_pct >= 0 ? "up" : "down"}">${t.return_pct >= 0 ? "+" : ""}${t.return_pct}%</td>
       <td>${t.bars_held}</td><td class="muted">${t.exit_reason}</td></tr>`).join("");
    $("#bt-note").textContent = d.note;
  } catch (e) {
    $("#bt-stats").innerHTML = `<div class="err">Backtest failed: ${e.message}</div>`;
  }
}

/* ---------------- backtest optimizer ---------------- */

$("#bt-optimize").addEventListener("click", async () => {
  $("#bt-ticker").textContent = currentTicker;
  $("#bt-opt-box").hidden = false;
  $("#bt-opt-table tbody").innerHTML = `<tr><td colspan="10" class="spin">Testing 36 configurations…</td></tr>`;
  try {
    const d = await api(`/api/optimize/${currentTicker}?period=${$("#bt-period").value}&direction=${$("#bt-dir").value}`);
    $("#bt-opt-table tbody").innerHTML = d.top.length ? d.top.map(r => `
      <tr><td>${r.buy_threshold}</td><td>${r.stop_atr}</td><td>${r.target_atr}</td>
      <td>${r.num_trades}</td><td>${r.win_rate}%</td><td>${r.profit_factor ?? "–"}</td>
      <td class="${r.expectancy_pct >= 0 ? "up" : "down"}">${r.expectancy_pct}%</td>
      <td>${r.total_return_pct}%</td><td>${r.max_drawdown_pct}%</td>
      <td><button class="use-cfg" data-b="${r.buy_threshold}" data-s="${r.stop_atr}" data-t="${r.target_atr}">Use</button></td></tr>`).join("")
      : `<tr><td colspan="10" class="muted">No configuration produced ≥8 trades — try a longer period.</td></tr>`;
    $("#bt-opt-warning").textContent = d.warning;
    document.querySelectorAll(".use-cfg").forEach(b => b.addEventListener("click", () => {
      $("#bt-buy").value = b.dataset.b; $("#bt-stop").value = b.dataset.s; $("#bt-target").value = b.dataset.t;
      runBacktest();
    }));
  } catch (e) {
    $("#bt-opt-table tbody").innerHTML = `<tr><td colspan="10" class="err">${e.message}</div></tr>`;
  }
});

/* ---------------- news ---------------- */

let newsItems = [];
let newsLoadedTicker = "";

function populateNewsTickerDropdown(selectedTicker) {
  const sel = $("#news-ticker");
  if (!sel) return;
  const mentioned = [...new Set([...WATCHLIST_17, ...newsItems.flatMap(n => n.tickers || [])])].sort();
  sel.innerHTML = `<option value="">All Market &amp; Tech (11 Feeds)</option>` +
    mentioned.map(t => `<option value="${t}" ${t === selectedTicker ? "selected" : ""}>${t}</option>`).join("");
}

async function loadNews(forceTicker) {
  const sel = $("#news-ticker");
  const tk = forceTicker !== undefined ? forceTicker : (sel ? sel.value : "");
  $("#news-list").innerHTML = `<p class="spin">Fetching live ${tk || "market & tech"} headlines…</p>`;
  try {
    const path = tk ? `/api/news?ticker=${encodeURIComponent(tk)}&limit=30` : "/api/news?limit=80";
    const d = await api(path);
    newsItems = d.items || [];
    newsLoadedTicker = tk;
    populateNewsTickerDropdown(tk);
    renderNews();
    $("#news-updated").textContent = `${newsItems.length} ${tk ? tk + " " : ""}headlines · updated ` + new Date().toLocaleTimeString();
  } catch (e) {
    $("#news-list").innerHTML = `<p class="err">News load failed: ${e.message}</p>`;
  }
}

function renderNews() {
  const show = {
    positive: $("#news-pos").checked,
    neutral: $("#news-neu").checked,
    negative: $("#news-neg").checked,
  };
  const tk = $("#news-ticker").value;
  if (tk !== newsLoadedTicker) {
    loadNews(tk);
    return;
  }
  const filtered = newsItems.filter(n => show[n.sentiment] && (!tk || (n.tickers || []).includes(tk)));
  const posCount = newsItems.filter(n => n.sentiment === "positive").length;
  const negCount = newsItems.filter(n => n.sentiment === "negative").length;
  const neuCount = newsItems.filter(n => n.sentiment === "neutral").length;
  const tone = negCount >= posCount + 2 ? "BEARISH CAUTION" : posCount >= negCount + 2 ? "BULLISH TAILWIND" : "BALANCED / NEUTRAL";
  const toneCls = tone.includes("BEARISH") ? "down" : tone.includes("BULLISH") ? "up" : "";

  const sumBox = $("#news-summary-tiles");
  if (sumBox) {
    const sampleNeg = newsItems.find(n => n.sentiment === "negative") || newsItems[0] || {};
    const ctxJson = esc(JSON.stringify({
      title: sampleNeg.title || "Live Market News",
      sentiment: sampleNeg.sentiment || "neutral",
      words: sampleNeg.sentiment_words || [],
      source: sampleNeg.source || "RSS",
      tickers: sampleNeg.tickers || (tk ? [tk] : [])
    }));
    sumBox.innerHTML = `
      <div class="tile" data-explain="headline" data-ctx="${ctxJson}">
        <div class="k">${userLevel === "beginner" ? "Overall News Mood" : "Net News Sentiment"}</div>
        <div class="v ${toneCls}">${tone}</div>
        <div class="muted">${tk ? "For " + tk : "Across 11 live feeds"}</div>
      </div>
      <div class="tile" data-explain="headline" data-ctx="${ctxJson}">
        <div class="k">Positive Headlines</div>
        <div class="v up">${posCount}</div>
        <div class="muted">Bullish catalyst stems</div>
      </div>
      <div class="tile" data-explain="headline" data-ctx="${ctxJson}">
        <div class="k">Negative Headlines</div>
        <div class="v ${negCount >= 2 && tk ? "down" : ""}">${negCount}</div>
        <div class="muted">${negCount >= 2 && tk ? "⚠ Cluster threshold (≥2)" : "Bearish risk stems"}</div>
      </div>
      <div class="tile" data-explain="headline" data-ctx="${ctxJson}">
        <div class="k">Neutral / Info</div>
        <div class="v">${neuCount}</div>
        <div class="muted">Ambient headlines</div>
      </div>`;
    bindExplainableElements(sumBox);
  }

  const listBox = $("#news-list");
  listBox.innerHTML = filtered.map(renderHeadlineRow).join("")
    || `<p class="muted">Nothing matches the current filters.</p>`;
  bindExplainableElements(listBox);
}

["news-pos", "news-neu", "news-neg"].forEach(id => $("#" + id).addEventListener("change", renderNews));
$("#news-ticker").addEventListener("change", () => loadNews($("#news-ticker").value));
if ($("#news-refresh-btn")) $("#news-refresh-btn").addEventListener("click", () => loadNews($("#news-ticker").value));

/* ---------------- journal ---------------- */

async function loadJournal() {
  // Per-user journal: require a signed-in account first.
  const me = await fetch("/api/auth/me", { headers: authHeaders() });
  if (!me.ok) {
    $("#j-auth").hidden = false;
    $("#j-main").style.display = "none";
    $("#j-logout").hidden = true;
    $("#j-user").textContent = "";
    return;
  }
  const user = await me.json();
  $("#j-auth").hidden = true;
  $("#j-main").style.display = "";
  $("#j-logout").hidden = false;
  $("#j-user").textContent = "signed in as " + user.username;
  loadPositions();
  loadInsights();
  pollAlerts();
  try {
    const [stats, list] = await Promise.all([api("/api/journal/stats"), api("/api/journal")]);
    $("#j-stats").innerHTML = [
      ["Closed trades", stats.total_trades],
      ["Open trades", stats.open_trades],
      ["Total P&L", fmt$(stats.total_pnl)],
      ["Win rate", (stats.win_rate ?? "–") + "%"],
      ["Avg win", fmt$(stats.avg_win)],
      ["Avg loss", fmt$(stats.avg_loss)],
      ["Profit factor", stats.profit_factor ?? "–"],
      ["Best / worst", `${fmt$(stats.best_trade)} / ${fmt$(stats.worst_trade)}`],
      // Provenance: broker-verified rows are the ones a client can be shown.
      ["Broker-verified / typed", `${stats.imported_trades ?? 0} / ${stats.manual_trades ?? 0}`],
      ["Needs review", stats.needs_review ?? 0],
    ].map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    if (stats.equity_curve.length) {
      drawChart("chart-pnl", stats.equity_curve.map(p => p.date || ""),
        [{ name: "Cum P&L", data: stats.equity_curve.map(p => p.cum_pnl), color: COLORS.s1 }],
        { refLines: [{ value: 0, color: COLORS.baseline }] });
    }
    $("#j-table tbody").innerHTML = list.trades.map(t => `
      <tr><td><b>${t.ticker}</b>${t.source ? ' <span class="pill" title="imported from a broker statement">✓</span>' : ""}${
        t.needs_review ? ` <span class="warn-text" title="${(t.review_note || "").replace(/"/g, "&quot;")}">⚠</span>` : ""}</td>
      <td>${t.instrument}${t.direction === "short" ? " (short)" : ""}</td>
      <td>${t.quantity}</td><td>${t.strike ?? "–"}</td>
      <td>${fmt$(t.entry_price)}</td><td>${t.entry_date}</td>
      <td>${t.exit_price === null ? '<span class="pill NEUTRAL">open</span>' : fmt$(t.exit_price)}</td>
      <td>${t.exit_date ?? "–"}</td>
      <td class="${(t.pnl ?? 0) >= 0 ? "up" : "down"}">${t.pnl === null ? "–" : fmt$(t.pnl)}</td>
      <td class="muted">${t.setup ?? ""}</td>
      <td><button class="del-btn" data-id="${t.id}" title="Delete">✕</button></td></tr>`).join("")
      || `<tr><td colspan="11" class="muted">No trades logged yet — add your first above.</td></tr>`;
    document.querySelectorAll(".del-btn").forEach(b => b.addEventListener("click", async () => {
      if (confirm("Delete this trade?")) { await fetch("/api/journal/" + b.dataset.id, { method: "DELETE", headers: authHeaders() }); loadJournal(); }
    }));
  } catch (e) {
    $("#j-stats").innerHTML = `<div class="err">${e.message}</div>`;
  }
}

$("#j-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  for (const [k, v] of fd.entries()) {
    if (v === "") continue;
    body[k] = ["quantity", "strike", "entry_price", "exit_price"].includes(k) ? parseFloat(v) : v;
  }
  const r = await fetch("/api/journal", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (r.ok) { e.target.reset(); loadJournal(); }
  else alert("Save failed: " + ((await r.json()).detail || r.statusText));
});

/* ---------------- broker statement import ---------------- */
// Two-step on purpose: preview first, commit second. Nothing touches the
// journal until the numbers on screen match what the broker says.

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("could not read that file"));
    r.readAsDataURL(file);
  });
}

async function runImport(dryRun) {
  const input = $("#j-import-file");
  const file = input.files && input.files[0];
  if (!file) { $("#j-import-msg").textContent = "pick a file first"; return; }

  $("#j-import-msg").textContent = dryRun ? "reading…" : "importing…";
  try {
    const r = await fetch("/api/journal/import", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        filename: file.name,
        content_b64: await fileToBase64(file),
        dry_run: dryRun,
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);

    const [lo, hi] = d.date_range || ["?", "?"];
    $("#j-import-msg").textContent = "";
    $("#j-import-commit").hidden = !d.dry_run || d.new_trades === 0;
    $("#j-import-result").innerHTML = `
      <div class="tiles">
        <div class="tile"><div class="k">Fills parsed</div><div class="v">${d.fills_parsed}</div></div>
        <div class="tile"><div class="k">New trades</div><div class="v">${d.new_trades}</div></div>
        <div class="tile"><div class="k">Closed / open</div><div class="v">${d.closed} / ${d.open}</div></div>
        <div class="tile"><div class="k">Net P&L</div>
          <div class="v ${d.net_pnl >= 0 ? "up" : "down"}">${fmt$(d.net_pnl)}</div></div>
        <div class="tile"><div class="k">Fees</div><div class="v">${fmt$(d.total_fees)}</div></div>
        <div class="tile"><div class="k">Already imported</div><div class="v">${d.skipped_duplicates}</div></div>
      </div>
      <p class="muted">${lo} → ${hi} · ${d.tickers.length} tickers${
        d.needs_review ? ` · <span class="warn-text">${d.needs_review} need review</span>` : ""}</p>
      ${d.dry_run ? `<p class="muted">Nothing written yet — check the rows below, then commit.</p>` : ""}
      ${(d.trades || []).length ? `<div class="scroll"><table>
        <thead><tr><th>Ticker</th><th>Position</th><th>Qty</th><th>Entry</th><th>Exit</th>
          <th>P&L</th><th>Flag</th></tr></thead><tbody>
        ${d.trades.map(t => `<tr>
          <td><b>${t.ticker}</b></td>
          <td>${t.direction === "short" ? "short " : ""}${t.instrument}${t.strike ? " $" + t.strike : ""}${t.expiry ? " " + t.expiry : ""}</td>
          <td>${t.quantity}</td>
          <td>${fmt$(t.entry_price)} <span class="muted">${t.entry_date}</span></td>
          <td>${t.exit_price === null ? '<span class="pill NEUTRAL">open</span>'
                : `${fmt$(t.exit_price)} <span class="muted">${t.exit_date}</span>`}</td>
          <td class="${(t.pnl ?? 0) >= 0 ? "up" : "down"}">${t.pnl === null ? "–" : fmt$(t.pnl)}</td>
          <td class="warn-text">${t.needs_review ? t.review_note : ""}</td></tr>`).join("")}
        </tbody></table></div>` : ""}`;

    if (!d.dry_run) {
      $("#j-import-msg").textContent = `imported ${d.written} trades`;
      $("#j-import-file").value = "";
      loadJournal();
    }
  } catch (e) {
    $("#j-import-msg").innerHTML = `<span class="err">${e.message}</span>`;
    $("#j-import-commit").hidden = true;
  }
}

$("#j-import-form").addEventListener("submit", (e) => { e.preventDefault(); runImport(true); });
$("#j-import-commit").addEventListener("click", () => runImport(false));

/* ---------------- live positions + coach ---------------- */

async function loadPositions() {
  try {
    const d = await api("/api/positions");
    const t = d.totals;
    $("#pos-totals").innerHTML = [
      ["Cost basis", fmt$(t.cost_basis)],
      ["Market value", fmt$(t.market_value)],
      ["Unrealized P&L", fmt$(t.unrealized_pnl)],
      ["Portfolio Δ (shares)", t.delta_shares],
      ["Portfolio Θ ($/day)", t.theta_per_day],
    ].map(([k, v]) => `<div class="tile"><div class="k">${k}</div>
      <div class="v ${typeof v === "string" && v.startsWith("-") ? "down" : ""}">${v}</div></div>`).join("");
    $("#pos-table tbody").innerHTML = d.positions.length ? d.positions.map(p => `
      <tr><td><b>${p.ticker}</b></td>
      <td>${p.direction === "short" ? "short " : ""}${p.quantity}x ${p.instrument}${p.strike ? " $" + p.strike : ""}${p.expiry ? " " + p.expiry : ""}</td>
      <td>${fmt$(p.entry_price)}</td><td>${p.mark !== null ? fmt$(p.mark) : '<span class="muted">n/a</span>'}</td>
      <td class="${(p.unrealized_pnl ?? 0) >= 0 ? "up" : "down"}">${p.unrealized_pnl !== null ? fmt$(p.unrealized_pnl) : "–"}</td>
      <td class="${(p.unrealized_pnl_pct ?? 0) >= 0 ? "up" : "down"}">${p.unrealized_pnl_pct !== null ? p.unrealized_pnl_pct + "%" : "–"}</td>
      <td>${p.delta_shares ?? "–"}</td><td>${p.theta_per_day ?? "–"}</td>
      <td class="${p.dte !== null && p.dte <= 7 ? "warn-text" : ""}">${p.dte ?? "–"}</td>
      <td class="warn-text">${p.flags.join("; ")}</td></tr>`).join("")
      : `<tr><td colspan="10" class="muted">No open positions — log an entry below and leave the exit blank.</td></tr>`;
    $("#pos-note").textContent = d.note;
    $("#pos-updated").textContent = "· updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("#pos-table tbody").innerHTML = `<tr><td colspan="10" class="err">${e.message}</td></tr>`;
  }
}

async function loadInsights() {
  try {
    const d = await api("/api/journal/insights");
    const bucketRows = (obj) => Object.entries(obj)
      .filter(([, v]) => v.count > 0)
      .map(([k, v]) => `<tr><td>${k}</td><td>${v.count}</td><td>${v.win_rate ?? "–"}%</td>
        <td class="${v.total_pnl >= 0 ? "up" : "down"}">${fmt$(v.total_pnl)}</td></tr>`).join("");
    $("#j-insights").innerHTML = `
      <ul class="reasons">${d.findings.map(f => `<li>${f}</li>`).join("")}</ul>
      <div class="grid2" style="margin-top:10px">
        <table><thead><tr><th>By type</th><th>Trades</th><th>Win rate</th><th>P&L</th></tr></thead>
          <tbody>${bucketRows(d.by_instrument) || '<tr><td colspan="4" class="muted">–</td></tr>'}</tbody></table>
        <table><thead><tr><th>By holding period</th><th>Trades</th><th>Win rate</th><th>P&L</th></tr></thead>
          <tbody>${bucketRows(d.by_holding_period) || '<tr><td colspan="4" class="muted">–</td></tr>'}</tbody></table>
      </div>`;
  } catch (e) {
    $("#j-insights").innerHTML = `<p class="err">${e.message}</p>`;
  }
}

/* ---------------- journal auth ---------------- */

$("#j-auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = e.submitter?.dataset.mode || "login";
  const fd = new FormData(e.target);
  const r = await fetch("/api/auth/" + mode, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: fd.get("username"), password: fd.get("password") }),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok) {
    localStorage.setItem("jtoken", d.token);
    $("#j-auth-msg").textContent = "";
    e.target.reset();
    loadJournal();
  } else {
    $("#j-auth-msg").textContent = d.detail || r.statusText;
  }
});

$("#j-logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST", headers: authHeaders() });
  localStorage.removeItem("jtoken");
  loadJournal();
});

/* ---------------- auto refresh ---------------- */

setInterval(() => {
  const tab = activeTab();
  if (tab === "dashboard") loadWatchlist();
  else if (tab === "town") loadTown();
  else if (tab === "agent") loadAgentBody();
  else if (tab === "analyze") loadAnalysis();
  else if (tab === "options") loadOptions($("#op-expiry").value || undefined);
}, 30000);
setInterval(() => { if (activeTab() === "news") loadNews(); }, 180000);
setInterval(pollAlerts, 60000);
setInterval(() => { if (activeTab() === "scanner") loadScanner(); }, 300000);
setInterval(() => {
  if (activeTab() === "journal" && !$("#j-main").style.display) loadPositions();
}, 60000);

/* ---------------- boot ---------------- */

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
syncLevelUI();
loadWatchlist();
loadNews();
pollAlerts();
