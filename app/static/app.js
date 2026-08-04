/* Options Trading Assistant — frontend. Plain JS, no dependencies. */
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

async function api(path) {
  const r = await fetch(path, { headers: authHeaders() });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

/* ---------------- tabs ---------------- */

document.querySelectorAll("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(name) {
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
  loadTab(name);
}

function activeTab() {
  return document.querySelector("#tabs button.active").dataset.tab;
}

function loadTab(name) {
  if (name === "dashboard") loadWatchlist();
  if (name === "scanner") loadScanner();
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

async function loadAgent(ticker) {
  const box = $("#agent-brief");
  box.innerHTML = `<p class="spin">Building agent brief for ${ticker} (signal + options + backtest + news)…</p>`;
  try {
    const d = await api("/api/agent/" + ticker);
    const sig = d.signal, opt = d.options || {}, bt = d.backtest || {}, rec = opt.recommendation || {};
    box.innerHTML = `
      <div class="signal-row">
        <span class="score-big">${d.ticker}</span>
        <span class="score-big ${sig.score >= 0 ? "up" : "down"}">${sig.score >= 0 ? "+" : ""}${sig.score}</span>
        <span class="pill ${pillClass(sig.label)}">${sig.label}</span>
        <span class="pill ${pillClass(rec.bias || "NO-TRADE")}">Options bias: ${rec.bias || "n/a"}</span>
        ${d.confluence ? `<span class="${d.confluence.state === "agree" ? "up" : d.confluence.state === "conflict" ? "down" : "muted"}">weekly: ${d.confluence.weekly_label} (${d.confluence.state})</span>` : ""}
        <span class="muted">price ${fmt$(d.quote.price)} (${d.quote.change_pct >= 0 ? "+" : ""}${d.quote.change_pct?.toFixed(2)}%)</span>
      </div>
      ${(rec.warnings || []).map(w => `<div class="warn-box">⚠ ${w}</div>`).join("")}
      <div class="tiles">
        <div class="tile"><div class="k">Next earnings</div><div class="v ${d.earnings?.days_until != null && d.earnings.days_until <= 14 ? "warn-text" : ""}">${d.earnings?.next_earnings ?? "–"}${d.earnings?.days_until != null ? ` <span class="muted">(${d.earnings.days_until}d)</span>` : ""}</div></div>
        <div class="tile"><div class="k">ATM IV</div><div class="v">${opt.atm_iv ?? "–"}%</div></div>
        <div class="tile"><div class="k">Expected move (${opt.dte ?? "?"} DTE)</div><div class="v">±${opt.expected_move ?? "–"} (${opt.expected_move_pct ?? "–"}%)</div></div>
        <div class="tile"><div class="k">Put/Call vol</div><div class="v">${opt.put_call_ratio_volume ?? "–"}</div></div>
        <div class="tile"><div class="k">Max pain</div><div class="v">${opt.max_pain ?? "–"}</div></div>
        <div class="tile"><div class="k">Backtest win rate</div><div class="v">${bt.win_rate ?? "–"}%</div></div>
        <div class="tile"><div class="k">Profit factor</div><div class="v">${bt.profit_factor ?? "–"}</div></div>
        ${d.scorecard?.buy?.count ? `<div class="tile"><div class="k">Buy-signal track record (3y)</div>
          <div class="v ${d.scorecard.buy.win_rate >= 55 ? "up" : d.scorecard.buy.win_rate <= 45 ? "down" : ""}">${d.scorecard.buy.win_rate}%</div>
          <div class="muted">${d.scorecard.buy.count} signals</div></div>` : ""}
        ${opt.iv_context ? `<div class="tile"><div class="k">IV context</div><div class="v">${opt.iv_context.label}</div>
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
      ${(d.news || []).slice(0, 6).map(n => `<div class="news-item"><span class="dot dot-${n.sentiment === "positive" ? "pos" : n.sentiment === "negative" ? "neg" : "neu"}"></span>
        <a href="${n.link}" target="_blank" rel="noopener">${n.title}</a><span class="news-meta">${n.source}</span></div>`).join("") || '<p class="muted">No headlines found.</p>'}
      ${(d.degraded || []).length ? `<p class="muted">Some sections were unavailable this run
        (${d.degraded.join(", ")}) — usually Yahoo rate-limiting. Everything else above is current;
        retry in a minute for the rest.</p>` : ""}
      <p class="muted">${d.plan.disclaimer}</p>`;
  } catch (e) {
    box.innerHTML = `<p class="err">Agent brief failed: ${e.message}</p>
      <p class="muted">This is almost always Yahoo Finance throttling the server rather than a bug
      in the app. Wait about a minute and click the ticker again — the Dashboard, Analyze and Learn
      tabs keep working from cache meanwhile.</p>
      <button onclick="loadAgent('${ticker}')">Retry now</button>`;
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

/* ---------------- analyze ---------------- */

async function loadAnalysis() {
  $("#an-ticker").textContent = currentTicker;
  const [period, interval] = $("#an-period").value.split("|");
  try {
    const d = await api(`/api/analysis/${currentTicker}?period=${period}&interval=${interval}`);
    const s = d.signal;
    $("#an-signal").innerHTML = `
      <span class="score-big ${s.score >= 0 ? "up" : "down"}">${s.score >= 0 ? "+" : ""}${s.score}</span>
      <span class="pill ${pillClass(s.label)}">${s.label}</span>
      <span class="muted">close ${fmt$(s.close)} · RSI ${s.rsi ?? "–"} · ATR ${s.atr ?? "–"}</span>`;
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
      `<tr><td>${v.component}</td><td>${v.weight}</td>
       <td class="${v.score >= 0 ? "up" : "down"}">${v.score >= 0 ? "+" : ""}${v.score}</td>
       <td class="muted">${v.reason}</td></tr>`).join("");
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
      ? `<div class="tile"><div class="k">${name} (${s.count} signals)</div>
         <div class="v ${s.win_rate >= 55 ? "up" : s.win_rate <= 45 ? "down" : ""}">${s.win_rate}% win</div>
         <div class="muted">avg ${s.avg_fwd_return_pct >= 0 ? "+" : ""}${s.avg_fwd_return_pct}% over ${d.horizon_days} bars</div></div>`
      : `<div class="tile"><div class="k">${name}</div><div class="v muted">no signals</div></div>`;
    box.innerHTML = `<div class="tiles">${row("BUY crossings", d.buy_signals)}${row("SELL crossings", d.sell_signals)}</div>
      ${d.buy_signals.recent.length ? `<table><thead><tr><th>Date</th><th>Score</th><th>Fwd return</th><th>Result</th></tr></thead>
      <tbody>${d.buy_signals.recent.map(e => `<tr><td>${e.date}</td><td>+${e.score}</td>
        <td class="${e.fwd_return_pct >= 0 ? "up" : "down"}">${e.fwd_return_pct >= 0 ? "+" : ""}${e.fwd_return_pct}%</td>
        <td>${e.win ? '<span class="up">✔ win</span>' : '<span class="down">✖ loss</span>'}</td></tr>`).join("")}</tbody></table>` : ""}
      <p class="muted">${d.note}</p>`;
  } catch (e) {
    box.innerHTML = `<p class="err">${e.message}</p>`;
  }
}

/* ---------------- options ---------------- */

async function loadOptions(expiry) {
  $("#op-ticker").textContent = currentTicker;
  $("#op-rec").innerHTML = `<p class="spin">Loading options chain…</p>`;
  try {
    const d = await api(`/api/options/${currentTicker}` + (expiry ? `?expiry=${expiry}` : ""));
    const sel = $("#op-expiry");
    sel.innerHTML = d.expiries.map(e => `<option ${e === d.expiry ? "selected" : ""}>${e}</option>`).join("");
    sel.onchange = () => loadOptions(sel.value);

    const rec = d.recommendation;
    $("#op-rec").innerHTML = `
      <div class="signal-row">
        <span class="pill ${pillClass(rec.bias)}">${rec.bias}</span>
        <span class="score-big ${rec.combined_score >= 0 ? "up" : "down"}">${rec.combined_score >= 0 ? "+" : ""}${rec.combined_score}</span>
        <span class="muted">combined stock + options score</span>
      </div>
      ${(rec.warnings || []).map(w => `<div class="warn-box">⚠ ${w}</div>`).join("")}
      ${rec.suggestion ? `<p><b>${rec.suggestion.action}</b> $${rec.suggestion.strike} exp ${rec.suggestion.expiry}
        — Δ ${rec.suggestion.delta}, Θ ${rec.suggestion.theta}/day, IV ${rec.suggestion.iv}%, mid ≈ ${fmt$(rec.suggestion.est_mid_price)}<br>
        <span class="muted">${rec.suggestion.note}</span></p>` : ""}
      ${rec.spread ? `<p><b>Defined-risk alternative — ${rec.spread.name}:</b>
        buy $${rec.spread.buy_strike} / sell $${rec.spread.sell_strike} for ≈ ${fmt$(rec.spread.net_debit)} debit.
        Max loss ${fmt$(rec.spread.max_loss)}, max profit ${fmt$(rec.spread.max_profit)}
        (${rec.spread.reward_risk}:1), breakeven ${rec.spread.breakeven}.<br>
        <span class="muted">${rec.spread.note}</span></p>` : ""}
      ${d.iv_context ? `<p class="muted">IV context: ${d.iv_context.note}</p>` : ""}
      <ul class="reasons">${rec.reasons.map(r => `<li>${r}</li>`).join("")}</ul>`;

    $("#op-metrics").innerHTML = [
      ["Spot", fmt$(d.spot)], ["DTE", d.dte],
      ["Next earnings", d.earnings?.next_earnings
        ? `${d.earnings.next_earnings}${d.earnings_before_expiry ? " ⚠" : ""}` : "–"],
      ["ATM IV", (d.atm_iv ?? "–") + "%"],
      ["Expected move", "±" + (d.expected_move ?? "–") + ` (${d.expected_move_pct ?? "–"}%)`],
      ["P/C ratio (vol)", d.put_call_ratio_volume ?? "–"],
      ["P/C ratio (OI)", d.put_call_ratio_oi ?? "–"],
      ["IV skew", (d.iv_skew ?? "–") + " pts"],
      ["IV context", d.iv_context ? `${d.iv_context.label} (${d.iv_context.percentile}%)` : "–"],
      ["Max pain", d.max_pain ?? "–"],
    ].map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

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
    $("#bt-opt-table tbody").innerHTML = `<tr><td colspan="10" class="err">${e.message}</td></tr>`;
  }
});

/* ---------------- news ---------------- */

let newsItems = [];

async function loadNews() {
  try {
    const d = await api("/api/news");
    newsItems = d.items;
    renderNews();
    $("#news-updated").textContent = `${newsItems.length} headlines · updated ` + new Date().toLocaleTimeString();
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
  const mentioned = [...new Set(newsItems.flatMap(n => n.tickers || []))].sort();
  const sel = $("#news-ticker");
  if (sel.options.length !== mentioned.length + 1) {
    sel.innerHTML = `<option value="">All</option>` +
      mentioned.map(t => `<option ${t === tk ? "selected" : ""}>${t}</option>`).join("");
  }
  $("#news-list").innerHTML = newsItems
    .filter(n => show[n.sentiment] && (!tk || (n.tickers || []).includes(tk)))
    .map(n =>
      `<div class="news-item"><span class="dot dot-${n.sentiment === "positive" ? "pos" : n.sentiment === "negative" ? "neg" : "neu"}"></span>
       <a href="${n.link}" target="_blank" rel="noopener">${n.title}</a>
       ${(n.tickers || []).map(t => `<span class="chip">${t}</span>`).join("")}
       <span class="news-meta">${n.source} · ${n.published_str} UTC</span></div>`).join("")
    || `<p class="muted">Nothing matches the current filters.</p>`;
}
["news-pos", "news-neu", "news-neg", "news-ticker"].forEach(id => $("#" + id).addEventListener("change", renderNews));

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
    ].map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    if (stats.equity_curve.length) {
      drawChart("chart-pnl", stats.equity_curve.map(p => p.date || ""),
        [{ name: "Cum P&L", data: stats.equity_curve.map(p => p.cum_pnl), color: COLORS.s1 }],
        { refLines: [{ value: 0, color: COLORS.baseline }] });
    }
    $("#j-table tbody").innerHTML = list.trades.map(t => `
      <tr><td><b>${t.ticker}</b></td><td>${t.instrument}${t.direction === "short" ? " (short)" : ""}</td>
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
loadWatchlist();
loadNews();
pollAlerts();
