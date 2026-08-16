/* Only Me & You — all client logic, no frameworks.
   The server is the source of truth for what's unlocked; this file's job is
   to make waiting for a sealed note feel like a present, not a 403. */

"use strict";

/* ---------------- tiny helpers ---------------- */

const $ = (id) => document.getElementById(id);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function setCookie(name, value, days = 180) {
  document.cookie = `${name}=${encodeURIComponent(value)}; max-age=${days * 86400}; path=/; samesite=lax`;
}

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------------- state ---------------- */

const state = {
  me: localStorage.getItem("omy_me") || "",
  pin: localStorage.getItem("omy_pin") || "",
  names: { a: "Me", b: "You" },
  anniversary: null,
  counts: null,
  clockOffset: 0,               // serverNow - deviceNow, so countdowns can't be cheated by a wrong phone clock
  heartsCursor: localStorage.getItem("omy_hearts_since") || "",
  photos: [],
  notes: { inbox: [], sent: [] },
  seg: "inbox",
  tab: "home",
  lastReadyCount: null,
  lastPhotoStamp: "",
  booted: false,
};

const nowSynced = () => Date.now() + state.clockOffset;
const myName = () => state.names[state.me] || "you";
const otherUser = () => (state.me === "a" ? "b" : "a");
const otherName = () => state.names[otherUser()] || "your love";

/* ---------------- api ---------------- */

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.me) headers["X-User"] = state.me;
  if (state.pin) headers["X-Pin"] = state.pin;
  let res;
  try {
    res = await fetch(path, Object.assign({}, opts, { headers }));
  } catch (err) {
    toast("Can't reach your little world 💔 is the server on?");
    throw err;
  }
  let body = {};
  try { body = await res.json(); } catch (err) { /* media/empty */ }
  if (res.status === 401) { showScreen("pin"); throw new Error("pin_required"); }
  if (!res.ok) {
    const msg = body && body.error ? body.error : `oops (${res.status})`;
    toast(msg);
    throw new Error(msg);
  }
  return body;
}

/* ---------------- formatting ---------------- */

const WDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
const MON3 = MONTHS.map((m) => m.slice(0, 3));

function fmtTime(d) {
  let h = d.getHours();
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return `${h}:${String(d.getMinutes()).padStart(2, "0")} ${ap}`;
}

function fmtClock(d) {
  let h = d.getHours();
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return `${h}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")} ${ap}`;
}

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
}

function fmtPretty(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  if (sameDay(d, now)) return `today ${fmtTime(d)}`;
  if (sameDay(d, tomorrow)) return `tomorrow ${fmtTime(d)}`;
  const yr = d.getFullYear() === now.getFullYear() ? "" : ` ${d.getFullYear()}`;
  return `${WDAYS[d.getDay()].slice(0, 3)}, ${MON3[d.getMonth()]} ${d.getDate()}${yr} · ${fmtTime(d)}`;
}

function fmtCountdown(ms) {
  if (ms <= 0) return "now!";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
    m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

function toLocalInput(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------------- toasts & fx ---------------- */

function toast(msg, happy = false) {
  const box = $("toasts");
  while (box.children.length >= 3) box.firstChild.remove();
  const el = document.createElement("div");
  el.className = "toast" + (happy ? " happy" : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("bye"); setTimeout(() => el.remove(), 350); }, 2800);
}

const HEART_SET = ["💖", "💗", "💘", "💝", "💕", "🩷", "✨"];

function burstAt(x, y, n = 10) {
  if (reducedMotion) return;
  const fx = $("fx");
  for (let i = 0; i < n; i++) {
    const el = document.createElement("div");
    el.className = "fx-heart";
    el.textContent = HEART_SET[i % HEART_SET.length];
    const ang = Math.random() * Math.PI * 2, dist = 50 + Math.random() * 90;
    el.style.left = `${x}px`; el.style.top = `${y}px`;
    el.style.fontSize = `${14 + Math.random() * 14}px`;
    el.style.setProperty("--dx", `${Math.cos(ang) * dist}px`);
    el.style.setProperty("--dy", `${Math.sin(ang) * dist - 40}px`);
    el.style.setProperty("--rot", `${(Math.random() * 60 - 30).toFixed(0)}deg`);
    fx.appendChild(el);
    setTimeout(() => el.remove(), 900);
  }
}

function confettiRain(n = 26) {
  if (reducedMotion) return;
  const fx = $("fx");
  for (let i = 0; i < n; i++) {
    const el = document.createElement("div");
    el.className = "fx-fall";
    el.textContent = HEART_SET[Math.floor(Math.random() * HEART_SET.length)];
    el.style.left = `${Math.random() * 100}vw`;
    el.style.fontSize = `${16 + Math.random() * 16}px`;
    el.style.setProperty("--dur", `${(2 + Math.random() * 1.4).toFixed(2)}s`);
    el.style.setProperty("--rot", `${(Math.random() * 240 - 120).toFixed(0)}deg`);
    el.style.animationDelay = `${(Math.random() * 0.5).toFixed(2)}s`;
    fx.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }
}

function popHeartAt(x, y) {
  if (reducedMotion) return;
  const el = document.createElement("div");
  el.className = "pop-heart";
  el.textContent = "❤️";
  el.style.left = `${x}px`; el.style.top = `${y}px`;
  $("fx").appendChild(el);
  setTimeout(() => el.remove(), 750);
}

/* ambient floating hearts on the home tab */
setInterval(() => {
  if (reducedMotion || document.hidden || !state.booted || state.tab !== "home") return;
  const el = document.createElement("div");
  el.className = "ambient-heart";
  el.textContent = HEART_SET[Math.floor(Math.random() * HEART_SET.length)];
  el.style.left = `${5 + Math.random() * 90}vw`;
  el.style.fontSize = `${13 + Math.random() * 12}px`;
  el.style.animationDuration = `${9 + Math.random() * 5}s`;
  $("ambient").appendChild(el);
  setTimeout(() => el.remove(), 15000);
}, 6500);

/* ---------------- screens ---------------- */

const SCREENS = ["loading", "setup", "pin", "who"];
function showScreen(name) {
  SCREENS.forEach((s) => $(`screen-${s}`).classList.toggle("hidden", s !== name));
  $("app").classList.toggle("hidden", name !== null);
  if (name === "who") {
    $("who-a-name").textContent = state.names.a;
    $("who-b-name").textContent = state.names.b;
  }
}

function enterApp() {
  SCREENS.forEach((s) => $(`screen-${s}`).classList.add("hidden"));
  $("app").classList.remove("hidden");
  state.booted = true;
  $("greeting").textContent = `Hi ${myName()} 💕`;
  $("idea-text").textContent = ideaOfTheDay();
  renderHomeStrips();
  renderUs();
  loadPhotos();
  loadNotes();
  renderInstallHelp();
}

/* ---------------- boot ---------------- */

async function boot() {
  if (state.pin) setCookie("omy_pin", state.pin);
  if (state.me) setCookie("omy_user", state.me);
  let st;
  try {
    st = await api(`/api/state${state.heartsCursor ? `?hearts_since=${encodeURIComponent(state.heartsCursor)}` : ""}`);
  } catch (err) {
    if (String(err.message) !== "pin_required") {
      $("screen-loading").innerHTML =
        '<div class="card setup-card"><div class="logo-heart">💔</div>' +
        "<p class=\"soft\">Can't reach the server.<br>Start it and refresh.</p></div>";
    }
    return;
  }
  absorbState(st);
  if (!st.setup) return showScreen("setup");
  if (st.pin_required && !st.authed) return showScreen("pin");
  if (!state.me) return showScreen("who");
  enterApp();
}

function absorbState(st) {
  if (st.now) state.clockOffset = new Date(st.now).getTime() - Date.now();
  if (st.names) state.names = st.names;
  if ("anniversary" in st) state.anniversary = st.anniversary;
  if (st.counts) {
    const prevReady = state.lastReadyCount;
    state.counts = st.counts;
    if (prevReady !== null && st.counts.inbox_ready > prevReady) {
      toast("A little surprise just unlocked 💝", true);
      confettiRain(14);
      if (state.tab === "notes") loadNotes();
    }
    state.lastReadyCount = st.counts.inbox_ready;
    const lp = st.counts.latest_photo;
    if (lp && state.lastPhotoStamp && lp.created_at > state.lastPhotoStamp && lp.by !== state.me) {
      toast(`${otherName()} added a new moment 📸`, true);
      if (state.tab === "moments") loadPhotos();
    }
    if (lp) state.lastPhotoStamp = lp.created_at;
    renderHomeStrips();
  }
  if (st.hearts && st.hearts.length && state.heartsCursor) {
    const n = st.hearts.length;
    toast(`${otherName()} sent you a heart 💗${n > 1 ? ` ×${n}` : ""}`, true);
    confettiRain(Math.min(10 + n * 4, 30));
  }
  if (st.now) {
    state.heartsCursor = st.now;
    localStorage.setItem("omy_hearts_since", st.now);
  }
}

/* setup */
$("setup-go").addEventListener("click", async () => {
  const name_a = $("setup-name-a").value.trim();
  const name_b = $("setup-name-b").value.trim();
  if (!name_a || !name_b) return toast("Both names, lovebirds 🕊️");
  const pin = $("setup-pin").value.trim();
  await api("/api/setup", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name_a, name_b,
      anniversary: $("setup-anniversary").value || "",
      pin,
    }),
  });
  state.names = { a: name_a, b: name_b };
  state.anniversary = $("setup-anniversary").value || null;
  if (pin) { state.pin = pin; localStorage.setItem("omy_pin", pin); setCookie("omy_pin", pin); }
  confettiRain(24);
  showScreen("who");
});

/* pin */
async function tryPin() {
  const pin = $("pin-input").value.trim();
  if (!pin) return;
  state.pin = pin;
  const st = await api("/api/state");
  if (st.authed) {
    localStorage.setItem("omy_pin", pin);
    setCookie("omy_pin", pin);
    $("pin-error").classList.add("hidden");
    absorbState(st);
    if (!state.me) return showScreen("who");
    enterApp();
  } else {
    state.pin = "";
    $("pin-error").classList.remove("hidden");
    $("pin-input").value = "";
  }
}
$("pin-go").addEventListener("click", tryPin);
$("pin-input").addEventListener("keydown", (e) => { if (e.key === "Enter") tryPin(); });

/* who */
for (const u of ["a", "b"]) {
  $(`who-${u}`).addEventListener("click", async () => {
    state.me = u;
    localStorage.setItem("omy_me", u);
    setCookie("omy_user", u);
    burstAt(innerWidth / 2, innerHeight / 2, 14);
    const st = await api("/api/state");
    absorbState(st);
    enterApp();
  });
}

/* ---------------- tabs ---------------- */

function goto(tab) {
  state.tab = tab;
  $$("#tabbar .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  ["home", "moments", "notes", "us"].forEach((t) =>
    $(`tab-${t}`).classList.toggle("hidden", t !== tab));
  window.scrollTo({ top: 0 });
  if (tab === "moments") loadPhotos();
  if (tab === "notes") loadNotes();
  if (tab === "us") renderUs();
}
$$("#tabbar .tab").forEach((b) => b.addEventListener("click", () => goto(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) goto(g.dataset.goto);
});

/* ---------------- clocks ---------------- */

function tickClocks() {
  const now = new Date();
  $("clock-date").textContent =
    `${WDAYS[now.getDay()]} · ${MONTHS[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
  $("clock-time").textContent = fmtClock(now);
  $("today-chip").textContent = `${MON3[now.getMonth()]} ${now.getDate()} · ${fmtTime(now)}`;

  const card = $("together-card");
  if (state.anniversary) {
    $("together-empty").classList.add("hidden");
    const [y, m, d] = state.anniversary.split("-").map(Number);
    const anniv = new Date(y, m - 1, d);
    let diff = now - anniv;
    const label = card.querySelector(".together-label");
    if (diff >= 0) { label.textContent = "together for"; }
    else { label.textContent = "counting down to our day 💍"; diff = -diff; }
    const days = Math.floor(diff / 86400000);
    const rest = Math.floor((diff % 86400000) / 1000);
    const h = Math.floor(rest / 3600), mi = Math.floor((rest % 3600) / 60), s = rest % 60;
    $("together-days").textContent = String(days);
    $("together-hms").textContent =
      `and ${h}h ${String(mi).padStart(2, "0")}m ${String(s).padStart(2, "0")}s 💗`;
  } else {
    $("together-days").textContent = "∞";
    $("together-hms").textContent = "";
    $("together-empty").classList.remove("hidden");
  }
}
setInterval(tickClocks, 1000);
tickClocks();

/* countdown chips re-tick every second */
function tickCountdowns() {
  let crossed = false;
  $$("[data-unlock]").forEach((el) => {
    const left = new Date(el.dataset.unlock).getTime() - nowSynced();
    const span = el.querySelector(".cd") || el;
    span.textContent = left <= 0 ? "now! 🎉" : fmtCountdown(left);
    el.classList.toggle("soon", left > 0 && left < 3600000);
    if (left <= 0 && el.dataset.armed !== "done") { el.dataset.armed = "done"; crossed = true; }
  });
  if (crossed) {
    confettiRain(12);
    toast("A sealed note just unlocked 💝", true);
    loadNotes();
    refreshState();
  }
}
setInterval(tickCountdowns, 1000);

/* ---------------- home ---------------- */

const IDEAS = [
  "💡 record a good-morning voice note and seal it for tomorrow 8am",
  "💡 add a photo of the first place you went together",
  "💡 send a video note sealed for Friday night 🎬",
  "💡 tell them one tiny thing you loved about today",
  "💡 seal a note for your next anniversary 💍",
  "💡 double-tap a moment to leave a ❤️",
  "💡 send a heart right now — it takes one tap 💗",
  "💡 add a photo of what you're doing right now",
  "💡 seal a bedtime story voice note for tonight 🌙",
  "💡 write a little letter they can open on a hard day",
];
function ideaOfTheDay() {
  const dayN = Math.floor(Date.now() / 86400000);
  return IDEAS[dayN % IDEAS.length];
}

function renderHomeStrips() {
  const c = state.counts;
  const strip = $("waiting-strip"), next = $("next-unlock-strip"), badge = $("notes-badge");
  if (!c) { strip.classList.add("hidden"); next.classList.add("hidden"); badge.classList.add("hidden"); return; }
  if (c.inbox_ready > 0) {
    $("waiting-text").textContent =
      c.inbox_ready === 1 ? "🎁 a little surprise is ready for you!"
        : `🎁 ${c.inbox_ready} little surprises are ready!`;
    strip.classList.remove("hidden");
    badge.textContent = c.inbox_ready;
    badge.classList.remove("hidden");
  } else {
    strip.classList.add("hidden");
    badge.classList.add("hidden");
  }
  if (c.next_unlock) {
    next.dataset.unlock = c.next_unlock;
    next.innerHTML = `⏳ next surprise unlocks in <b class="cd"></b>`;
    delete next.dataset.armed;
    next.classList.remove("hidden");
  } else {
    delete next.dataset.unlock;
    next.classList.add("hidden");
  }
  tickCountdowns();
}

$("heart-btn").addEventListener("click", async (e) => {
  burstAt(e.clientX || innerWidth / 2, e.clientY || innerHeight / 2, 12);
  await api("/api/hearts", { method: "POST" });
  toast(`💗 heart sent to ${otherName()}!`, true);
});

$("qa-photo").addEventListener("click", () => $("file-photo").click());
$("add-moment").addEventListener("click", () => $("file-photo").click());
$("qa-voice").addEventListener("click", () => openCompose("voice"));
$("qa-video").addEventListener("click", () => openCompose("video"));
$("qa-text").addEventListener("click", () => openCompose("text"));
$("add-note").addEventListener("click", () => openCompose("text"));

/* ---------------- moments ---------------- */

async function loadPhotos() {
  const data = await api("/api/photos");
  state.photos = data.photos;
  renderPhotos();
}

function renderPhotos() {
  const list = $("moments-list");
  const photos = state.photos;
  $("moments-empty").classList.toggle("hidden", photos.length > 0);
  if (!photos.length) { list.innerHTML = ""; return; }
  const groups = [];
  let current = null;
  for (const p of photos) {
    const d = new Date(p.at);
    const key = `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
    if (!current || current.key !== key) { current = { key, items: [] }; groups.push(current); }
    current.items.push(p);
  }
  list.innerHTML = groups.map((g) => `
    <div class="month-head">${esc(g.key)}</div>
    <div class="moments-grid">
      ${g.items.map((p) => {
        const d = new Date(p.at);
        const rx = Object.values(p.reactions || {}).join("");
        return `
        <figure class="photo-card" data-photo="${p.id}">
          <img src="${p.media_url}" alt="${esc(p.caption || "our moment")}" loading="lazy">
          <span class="photo-day">${MON3[d.getMonth()]} ${d.getDate()}</span>
          <figcaption class="photo-foot">
            <span class="cap">${esc(p.caption || "")}</span>
            <span class="rx">${rx}</span>
          </figcaption>
        </figure>`;
      }).join("")}
    </div>`).join("");
}

$("moments-list").addEventListener("click", (e) => {
  const card = e.target.closest("[data-photo]");
  if (card) openLightbox(Number(card.dataset.photo));
});

/* upload flow */
let pendingFiles = [];
$("file-photo").addEventListener("change", () => {
  pendingFiles = Array.from($("file-photo").files || []);
  $("file-photo").value = "";
  if (!pendingFiles.length) return;
  $("upload-title").textContent =
    pendingFiles.length === 1 ? "Add a moment 📸" : `Add ${pendingFiles.length} moments 📸`;
  $("upload-previews").innerHTML = "";
  for (const f of pendingFiles.slice(0, 8)) {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    $("upload-previews").appendChild(img);
  }
  $("upload-caption").value = "";
  $("upload-when").value = toLocalInput(new Date());
  $("upload-progress").classList.add("hidden");
  $("modal-upload").classList.remove("hidden");
});

$("upload-cancel").addEventListener("click", () => closeUpload());
function closeUpload() {
  $$("#upload-previews img").forEach((i) => URL.revokeObjectURL(i.src));
  $("modal-upload").classList.add("hidden");
  pendingFiles = [];
}

$("upload-go").addEventListener("click", async () => {
  if (!pendingFiles.length) return closeUpload();
  const caption = $("upload-caption").value.trim();
  const whenVal = $("upload-when").value;
  const takenIso = whenVal ? new Date(whenVal).toISOString() : "";
  const prog = $("upload-progress");
  prog.classList.remove("hidden");
  $("upload-go").disabled = true;
  try {
    for (let i = 0; i < pendingFiles.length; i++) {
      prog.textContent = `sending ${i + 1} of ${pendingFiles.length}… 💌`;
      const f = pendingFiles[i];
      const qs = new URLSearchParams({ caption, fn: f.name || "" });
      if (takenIso) qs.set("taken_at", takenIso);
      await api(`/api/photos?${qs}`, {
        method: "POST",
        headers: { "Content-Type": f.type || "application/octet-stream" },
        body: f,
      });
    }
    toast(pendingFiles.length === 1 ? "moment saved 📸💖" : "moments saved 📸💖", true);
    confettiRain(14);
    loadPhotos();
    refreshState();
    goto("moments");
  } finally {
    $("upload-go").disabled = false;
    closeUpload();
  }
});

/* lightbox */
const REACT_EMOJIS = ["❤️", "😍", "🥰", "😂", "🥺", "🔥"];
let lightboxId = null;
let lastTap = 0;

function openLightbox(id) {
  const p = state.photos.find((x) => x.id === id);
  if (!p) return;
  lightboxId = id;
  const d = new Date(p.at);
  $("lb-img-wrap").innerHTML =
    `<img src="${p.media_url}" alt="${esc(p.caption || "our moment")}">`;
  $("lb-caption").textContent = p.caption || "";
  const byName = state.names[p.by] || "?";
  let when = `by ${byName} · ${WDAYS[d.getDay()]}, ${MON3[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} · ${fmtTime(d)}`;
  const orx = (p.reactions || {})[otherUser()];
  if (orx) when += ` · ${otherName()} reacted ${orx}`;
  $("lb-when").textContent = when;
  const mine = (p.reactions || {})[state.me];
  $("lb-react-row").innerHTML = REACT_EMOJIS.map((em) =>
    `<button class="react-btn${em === mine ? " mine" : ""}" data-em="${em}">${em}</button>`).join("");
  $("modal-lightbox").classList.remove("hidden");
}

$("lb-close").addEventListener("click", () => $("modal-lightbox").classList.add("hidden"));
$("modal-lightbox").addEventListener("click", (e) => {
  if (e.target === $("modal-lightbox")) $("modal-lightbox").classList.add("hidden");
});

$("lb-react-row").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-em]");
  if (!btn || lightboxId == null) return;
  const p = state.photos.find((x) => x.id === lightboxId);
  const mine = (p.reactions || {})[state.me];
  const emoji = btn.dataset.em === mine ? "" : btn.dataset.em;
  if (emoji) burstAt(e.clientX, e.clientY, 7);
  const out = await api(`/api/photos/${lightboxId}/react`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ emoji }),
  });
  const idx = state.photos.findIndex((x) => x.id === lightboxId);
  if (idx >= 0) state.photos[idx] = out.photo;
  openLightbox(lightboxId);
  renderPhotos();
});

$("lb-img-wrap").addEventListener("click", async (e) => {
  const nowT = Date.now();
  if (nowT - lastTap < 320 && lightboxId != null) {
    popHeartAt(e.clientX, e.clientY);
    const out = await api(`/api/photos/${lightboxId}/react`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ emoji: "❤️" }),
    });
    const idx = state.photos.findIndex((x) => x.id === lightboxId);
    if (idx >= 0) state.photos[idx] = out.photo;
    openLightbox(lightboxId);
    renderPhotos();
  }
  lastTap = nowT;
});

$("lb-delete").addEventListener("click", async () => {
  if (lightboxId == null) return;
  if (!confirm("Delete this moment for both of you?")) return;
  await api(`/api/photos/${lightboxId}`, { method: "DELETE" });
  $("modal-lightbox").classList.add("hidden");
  toast("moment deleted 🥀");
  loadPhotos();
  refreshState();
});

/* ---------------- notes list ---------------- */

async function loadNotes() {
  if (!state.me) return;
  const data = await api("/api/notes");
  state.notes = data;
  renderNotes();
  renderHomeStrips();
}

const KIND_META = {
  voice: { emoji: "🎤", word: "a voice note" },
  video: { emoji: "🎬", word: "a little video" },
  text: { emoji: "💌", word: "a little letter" },
};

function noteMediaEl(n) {
  if (!n.media_url) return "";
  if (n.kind === "video") {
    return `<video controls playsinline preload="metadata" src="${n.media_url}"></video>`;
  }
  return `<audio controls preload="metadata" src="${n.media_url}"></audio>`;
}

function renderNotes() {
  $("seg-inbox").classList.toggle("active", state.seg === "inbox");
  $("seg-sent").classList.toggle("active", state.seg === "sent");
  const list = $("notes-list");
  const items = state.notes[state.seg] || [];
  $("notes-empty").classList.toggle("hidden", items.length > 0);
  $("notes-empty-text").innerHTML = state.seg === "inbox"
    ? "Nothing for you yet…<br>maybe drop a hint 😉"
    : "You haven't sent one yet…<br>seal the first little surprise!";
  if (!items.length) { list.innerHTML = ""; return; }

  if (state.seg === "inbox") {
    const ready = items.filter((n) => !n.locked && !n.opened_at);
    const locked = items.filter((n) => n.locked)
      .sort((x, y2) => x.unlock_at.localeCompare(y2.unlock_at));
    const opened = items.filter((n) => !n.locked && n.opened_at);
    list.innerHTML = [
      ...ready.map((n) => `
        <div class="note-card ready" data-note="${n.id}">
          <div class="note-top">
            <div class="note-emoji">🎁</div>
            <div><div class="note-who">from ${esc(otherName())} 💝</div>
              <div class="note-sub">${KIND_META[n.kind].word} · sealed ${fmtPretty(n.created_at)}</div></div>
            <div class="note-count">ready!</div>
          </div>
          <button class="btn btn-primary note-open-btn" data-open="${n.id}">Open it 🎀</button>
        </div>`),
      ...locked.map((n) => `
        <div class="note-card locked" data-note="${n.id}" data-locked="1">
          <div class="note-top">
            <div class="note-emoji" data-wiggle="${n.id}">🎁</div>
            <div><div class="note-who">from ${esc(otherName())}</div>
              <div class="note-sub">${KIND_META[n.kind].word} is waiting…</div></div>
            <div class="note-count" data-unlock="${n.unlock_at}">opens in <b class="cd"></b></div>
          </div>
          <div class="note-status">🔒 opens ${fmtPretty(n.unlock_at)}</div>
        </div>`),
      ...opened.map((n) => `
        <div class="note-card" data-note="${n.id}">
          <div class="note-top">
            <div class="note-emoji">${KIND_META[n.kind].emoji}</div>
            <div><div class="note-who">from ${esc(otherName())}</div>
              <div class="note-sub">opened ${fmtPretty(n.opened_at)}</div></div>
          </div>
          <div class="note-body">
            ${n.text ? `<div class="note-text-bubble">${esc(n.text)}</div>` : ""}
            <div class="note-media">${noteMediaEl(n)}</div>
          </div>
        </div>`),
    ].join("");
  } else {
    list.innerHTML = items.map((n) => {
      const status = n.locked
        ? `🔒 sealed · opens ${fmtPretty(n.unlock_at)}`
        : n.opened_at
          ? `<span class="opened">opened ${fmtPretty(n.opened_at)} 💗</span>`
          : `unlocked · not opened yet 👀`;
      return `
        <div class="note-card" data-note="${n.id}">
          <div class="note-top">
            <div class="note-emoji">${KIND_META[n.kind].emoji}</div>
            <div><div class="note-who">to ${esc(otherName())}</div>
              <div class="note-sub">${KIND_META[n.kind].word} · ${fmtPretty(n.created_at)}</div></div>
            ${n.locked ? `<div class="note-count" data-unlock="${n.unlock_at}"><b class="cd"></b></div>` : ""}
          </div>
          <div class="note-body">
            ${n.text ? `<div class="note-text-bubble">${esc(n.text)}</div>` : ""}
            <div class="note-media">${noteMediaEl(n)}</div>
          </div>
          <div class="note-status">${status}</div>
        </div>`;
    }).join("");
  }
  tickCountdowns();
}

$("seg-inbox").addEventListener("click", () => { state.seg = "inbox"; renderNotes(); });
$("seg-sent").addEventListener("click", () => { state.seg = "sent"; renderNotes(); });


$("notes-list").addEventListener("click", async (e) => {
  const openBtn = e.target.closest("[data-open]");
  if (openBtn) return openNoteCeremony(Number(openBtn.dataset.open));
  const lockedCard = e.target.closest('[data-locked="1"]');
  if (lockedCard) {
    const id = lockedCard.dataset.note;
    const em = lockedCard.querySelector(`[data-wiggle="${id}"]`);
    if (em) { em.classList.remove("wiggle"); void em.offsetWidth; em.classList.add("wiggle"); }
    const at = lockedCard.querySelector("[data-unlock]").dataset.unlock;
    toast(`🤫 not yet! opens ${fmtPretty(at)}`);
  }
});

async function openNoteCeremony(id) {
  const out = await api(`/api/notes/${id}/open`, { method: "POST" });
  const n = out.note;
  confettiRain(30);
  $("open-head").textContent = `💌 from ${otherName()}, sealed ${fmtPretty(n.created_at)}`;
  $("open-media").innerHTML = n.media_url
    ? (n.kind === "video"
      ? `<video controls autoplay playsinline src="${n.media_url}"></video>`
      : `<audio controls autoplay src="${n.media_url}"></audio>`)
    : "";
  const textEl = $("open-text");
  textEl.classList.toggle("hidden", !n.text);
  textEl.textContent = n.text || "";
  $("modal-open").classList.remove("hidden");
  loadNotes();
  refreshState();
}
$("open-close").addEventListener("click", () => {
  $("open-media").innerHTML = "";
  $("modal-open").classList.add("hidden");
});

/* ---------------- compose ---------------- */

const compose = {
  mode: null, blob: null, blobName: "", unlockAt: null,
  rec: null, stream: null, chunks: [], timer: null, t0: 0,
};

function openCompose(mode) {
  compose.mode = mode;
  compose.blob = null; compose.blobName = "";
  compose.unlockAt = null;
  $("compose-title").textContent =
    mode === "voice" ? "A voice note 🎤" : mode === "video" ? "A little video 🎬" : "A little letter 💌";
  $("unlock-who").textContent = otherName();
  $("compose-record").classList.toggle("hidden", mode !== "voice");
  $("compose-video-pick").classList.toggle("hidden", mode !== "video");
  $("compose-textwrap").classList.toggle("hidden", mode !== "text");
  $("compose-caption-wrap").classList.add("hidden");
  $("compose-preview").classList.add("hidden");
  $("compose-preview").innerHTML = "";
  $("compose-unlock").classList.toggle("hidden", mode !== "text");
  $("compose-text").value = "";
  $("compose-caption").value = "";
  $("unlock-custom").value = "";
  $("unlock-custom").min = toLocalInput(new Date());
  $("compose-progress").classList.add("hidden");
  $$(".unlock-opt").forEach((b) => b.classList.remove("picked"));
  pickUnlock("now", false);
  $("rec-timer").textContent = "tap to record";
  $("rec-btn").classList.remove("recording");
  $("rec-btn").textContent = "🎤";
  const canRec = window.isSecureContext && navigator.mediaDevices &&
    navigator.mediaDevices.getUserMedia && window.MediaRecorder;
  $("rec-fallback").classList.toggle("hidden", mode !== "voice" || !!canRec);
  if (mode === "voice" && !canRec) {
    $("rec-btn").disabled = true;
    $("rec-timer").textContent = "mic needs an HTTPS link 🌐 — pick a file instead";
    $("rec-fallback").classList.remove("hidden");
  } else {
    $("rec-btn").disabled = false;
  }
  updateComposeSend();
  $("modal-compose").classList.remove("hidden");
}

function updateComposeSend() {
  const has = compose.mode === "text"
    ? $("compose-text").value.trim().length > 0
    : !!compose.blob;
  $("compose-send").disabled = !(has && compose.unlockAt);
}
$("compose-text").addEventListener("input", updateComposeSend);

$("emoji-row").addEventListener("click", (e) => {
  if (e.target.tagName === "BUTTON") {
    $("compose-text").value += e.target.textContent;
    updateComposeSend();
  }
});

/* unlock picking */
function pickUnlock(opt, showChip = true) {
  const now = new Date();
  let when = null;
  if (opt === "now") when = now;
  else if (opt === "hour") when = new Date(now.getTime() + 3600000);
  else if (opt === "tonight") {
    when = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 20, 0);
    if (when - now < 15 * 60000) when.setDate(when.getDate() + 1);
  } else if (opt === "morning") {
    when = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0);
  }
  compose.unlockAt = when;
  $$(".unlock-opt").forEach((b) => b.classList.toggle("picked", b.dataset.unlockopt === opt));
  if (showChip) $("unlock-custom").value = "";
  $("unlock-chosen").textContent =
    opt === "now" ? `they can open it right away 💌` : `sealed until ${fmtPretty(when)} 🔒`;
  updateComposeSend();
}
$$(".unlock-opt").forEach((b) =>
  b.addEventListener("click", () => pickUnlock(b.dataset.unlockopt)));
$("unlock-custom").addEventListener("change", () => {
  const v = $("unlock-custom").value;
  if (!v) return;
  compose.unlockAt = new Date(v);
  $$(".unlock-opt").forEach((x) => x.classList.remove("picked"));
  $("unlock-chosen").textContent = `sealed until ${fmtPretty(compose.unlockAt)} 🔒`;
  updateComposeSend();
});

/* voice recording */
function pickAudioMime() {
  const opts = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  for (const t of opts) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

$("rec-btn").addEventListener("click", async () => {
  if (compose.rec && compose.rec.state === "recording") return stopRecording();
  try {
    compose.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    toast("mic said no 🙊 — allow the microphone or pick a file");
    $("rec-fallback").classList.remove("hidden");
    return;
  }
  const mime = pickAudioMime();
  compose.chunks = [];
  compose.rec = new MediaRecorder(compose.stream, mime ? { mimeType: mime } : {});
  compose.rec.ondataavailable = (e) => { if (e.data.size) compose.chunks.push(e.data); };
  compose.rec.onstop = () => {
    const type = compose.rec.mimeType || mime || "audio/webm";
    compose.blob = new Blob(compose.chunks, { type });
    compose.blobName = "voice-note";
    compose.stream.getTracks().forEach((t) => t.stop());
    showComposePreview();
  };
  compose.rec.start();
  compose.t0 = Date.now();
  $("rec-btn").classList.add("recording");
  $("rec-btn").textContent = "⏹";
  compose.timer = setInterval(() => {
    const s = Math.floor((Date.now() - compose.t0) / 1000);
    $("rec-timer").textContent =
      `recording… ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} 🔴`;
    if (s >= 300) stopRecording();  // five minutes is a podcast, love
  }, 250);
});

function stopRecording() {
  if (compose.timer) clearInterval(compose.timer);
  $("rec-btn").classList.remove("recording");
  $("rec-btn").textContent = "🎤";
  $("rec-timer").textContent = "tap to record";
  if (compose.rec && compose.rec.state === "recording") compose.rec.stop();
}

$("rec-fallback").addEventListener("click", () => $("file-audio").click());
$("file-audio").addEventListener("change", () => {
  const f = $("file-audio").files[0];
  $("file-audio").value = "";
  if (f) { compose.blob = f; compose.blobName = f.name; showComposePreview(); }
});

/* video picking */
$("vid-record").addEventListener("click", () => $("file-video-rec").click());
$("vid-choose").addEventListener("click", () => $("file-video").click());
for (const id of ["file-video-rec", "file-video"]) {
  $(id).addEventListener("change", () => {
    const f = $(id).files[0];
    $(id).value = "";
    if (!f) return;
    if (f.size > 80 * 1024 * 1024) return toast("that video is a bit too big 🙈 (80 MB max)");
    compose.blob = f; compose.blobName = f.name;
    showComposePreview();
  });
}

function showComposePreview() {
  const wrap = $("compose-preview");
  const url = URL.createObjectURL(compose.blob);
  const isVideo = compose.mode === "video";
  wrap.innerHTML = isVideo
    ? `<video controls playsinline src="${url}"></video>`
    : `<audio controls src="${url}"></audio>`;
  const redo = document.createElement("button");
  redo.className = "btn btn-ghost btn-small redo";
  redo.textContent = "start over 🔄";
  redo.addEventListener("click", () => {
    URL.revokeObjectURL(url);
    compose.blob = null;
    wrap.classList.add("hidden");
    wrap.innerHTML = "";
    $("compose-caption-wrap").classList.add("hidden");
    $("compose-unlock").classList.add("hidden");
    if (compose.mode === "voice") $("compose-record").classList.remove("hidden");
    if (compose.mode === "video") $("compose-video-pick").classList.remove("hidden");
    updateComposeSend();
  });
  wrap.appendChild(redo);
  wrap.classList.remove("hidden");
  $("compose-record").classList.add("hidden");
  $("compose-video-pick").classList.add("hidden");
  $("compose-caption-wrap").classList.remove("hidden");
  $("compose-unlock").classList.remove("hidden");
  updateComposeSend();
}

function closeCompose() {
  stopRecording();
  if (compose.stream) compose.stream.getTracks().forEach((t) => t.stop());
  $$("#compose-preview audio, #compose-preview video").forEach((m) => {
    try { URL.revokeObjectURL(m.src); } catch (err) { /* fine */ }
  });
  $("modal-compose").classList.add("hidden");
}
$("compose-cancel").addEventListener("click", closeCompose);

$("compose-send").addEventListener("click", async () => {
  if (!compose.unlockAt) return;
  const unlockIso = compose.unlockAt.toISOString();
  const prog = $("compose-progress");
  prog.textContent = "sealing it… 💝";
  prog.classList.remove("hidden");
  $("compose-send").disabled = true;
  try {
    if (compose.mode === "text") {
      await api("/api/notes", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "text", text: $("compose-text").value.trim(), unlock_at: unlockIso,
        }),
      });
    } else {
      const qs = new URLSearchParams({
        kind: compose.mode, unlock_at: unlockIso,
        text: $("compose-caption").value.trim(), fn: compose.blobName || "",
      });
      await api(`/api/notes?${qs}`, {
        method: "POST",
        headers: { "Content-Type": compose.blob.type || "application/octet-stream" },
        body: compose.blob,
      });
    }
    const soon = compose.unlockAt - new Date() < 60000;
    toast(soon ? `sent! ${otherName()} can open it now 💌` : `sealed 💝 opens ${fmtPretty(compose.unlockAt)}`, true);
    confettiRain(16);
    closeCompose();
    state.seg = "sent";
    goto("notes");
  } catch (err) {
    prog.classList.add("hidden");
    $("compose-send").disabled = false;
  }
});

/* ---------------- us / settings ---------------- */

function renderUs() {
  $("us-name-a").value = state.names.a;
  $("us-name-b").value = state.names.b;
  $("us-anniversary").value = state.anniversary || "";
  $("us-me").textContent = `${myName()} ${state.me === "a" ? "🩷" : "💜"}`;
  $("us-pin-state").textContent = state.pin
    ? "A PIN is protecting your little world ✨"
    : "No PIN yet — anyone on your network could peek 👀";
}

$("us-save").addEventListener("click", async () => {
  const out = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name_a: $("us-name-a").value.trim(),
      name_b: $("us-name-b").value.trim(),
      anniversary: $("us-anniversary").value || "",
    }),
  });
  state.names = { a: out.couple.name_a, b: out.couple.name_b };
  state.anniversary = out.couple.anniversary;
  $("greeting").textContent = `Hi ${myName()} 💕`;
  toast("saved 💾💕", true);
  renderUs();
});

$("us-pin-save").addEventListener("click", async () => {
  const pin = $("us-pin").value.trim();
  await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin }),
  });
  state.pin = pin;
  if (pin) { localStorage.setItem("omy_pin", pin); setCookie("omy_pin", pin); }
  else { localStorage.removeItem("omy_pin"); setCookie("omy_pin", ""); }
  $("us-pin").value = "";
  toast(pin ? "PIN set 🔒 tell only one person 😉" : "PIN removed 🔓", true);
  renderUs();
});

$("us-switch").addEventListener("click", () => {
  localStorage.removeItem("omy_me");
  state.me = "";
  setCookie("omy_user", "");
  showScreen("who");
});

/* install help */
let deferredInstall = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredInstall = e;
  const btn = $("install-btn");
  if (btn) btn.classList.remove("hidden");
});

function renderInstallHelp() {
  const standalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  const el = $("install-steps");
  if (standalone) {
    el.innerHTML = "You're using the installed app — all set ✨";
    return;
  }
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  el.innerHTML = isIOS
    ? `On iPhone: open this page in <b>Safari</b> → tap <b>Share</b> (the square with the arrow) → <b>Add to Home Screen</b> → <b>Add</b>. A 💞 icon appears like a real app!`
    : `On Android: open this page in <b>Chrome</b> → tap <b>⋮</b> → <b>Add to Home screen</b> (or <b>Install app</b>). A 💞 icon appears like a real app!`;
  if (!window.isSecureContext) {
    el.innerHTML += `<br><br>🌐 Tip: over plain http the mic recorder and full install are limited — the README shows how to get a free https link.`;
  }
}

$("install-btn").addEventListener("click", async () => {
  if (!deferredInstall) return;
  deferredInstall.prompt();
  await deferredInstall.userChoice;
  deferredInstall = null;
  $("install-btn").classList.add("hidden");
});

/* ---------------- polling ---------------- */

async function refreshState() {
  if (!state.booted) return;
  try {
    const st = await api(`/api/state?hearts_since=${encodeURIComponent(state.heartsCursor || "")}`);
    absorbState(st);
  } catch (err) { /* quiet — toast already shown for real errors */ }
}
setInterval(refreshState, 12000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.booted) {
    refreshState();
    if (state.tab === "moments") loadPhotos();
    if (state.tab === "notes") loadNotes();
  }
});

/* ---------------- service worker ---------------- */

if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* http LAN — fine */ });
  });
}

/* ---------------- go ---------------- */

boot();
