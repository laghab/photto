"use strict";

// ------------------------------------------------------------ setup -----
const HASH = new URLSearchParams(location.hash.slice(1));
const TOKEN = HASH.get("t") || "";
const DEBUG = HASH.get("debug") === "1";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const url = (route, p) => `${route}?p=${encodeURIComponent(p)}&t=${encodeURIComponent(TOKEN)}`;

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

const GROUPS = [
  { id: "all", label: "Everything", kinds: null },
  { id: "photos", label: "Photos", kinds: ["image", "image-convert"] },
  { id: "videos", label: "Videos", kinds: ["video"] },
  { id: "audio", label: "Audio", kinds: ["audio"] },
  { id: "docs", label: "Docs", kinds: ["pdf", "text", "doc"] },
  { id: "other", label: "Other", kinds: ["archive", "other"] },
];
const COLORS = {
  image: ["#ffb86b", "#ff7a6b"], "image-convert": ["#ffb86b", "#ff7a6b"],
  video: ["#b69cff", "#6b8cff"], audio: ["#ffcf5c", "#ff9f43"],
  pdf: ["#ff9c8c", "#e05555"], text: ["#9be7c4", "#4fb3a0"],
  doc: ["#8ec5ff", "#4f8bd6"], archive: ["#e2cfa9", "#9c8661"], other: ["#d4cdc2", "#8a8378"],
};
const colorVars = (k) => `--g1:${COLORS[k][0]};--g2:${COLORS[k][1]}`;
const groupOf = (kind) => (GROUPS.find((g) => g.kinds && g.kinds.includes(kind)) || { id: "other" }).id;
const inGroup = (f, g = S.filter) => { const k = GROUPS.find((x) => x.id === g).kinds; return !k || k.includes(f.kind); };
const versionOf = (f) => `${Math.floor((f.mtime || 0) * 1000)}-${f.size || 0}`;
const thumbUrl = (f, prio = 2) => `${url("/thumb", f.path)}&v=${encodeURIComponent(versionOf(f))}&prio=${prio}`;

const PAGE = 100;
const LOW_WATER = 12;
const REVIEW_PAGE = 100;
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

const T0 = performance.now();
const PERF = { firstPageMs: null, firstCardMs: null, swipeToNextMs: null, reviewOpenMs: null };
if (DEBUG) window.__phottoPerf = PERF;
let firstPageMarked = false;
let firstCardMarked = false;
let reportTimer;

function perfValue(name, value = performance.now() - T0) {
  PERF[name] = Math.round(value * 10) / 10;
  try { performance.mark(`photto:${name.replace(/Ms$/, "").replace(/[A-Z]/g, (c) => "-" + c.toLowerCase())}`); } catch { /* ignore */ }
  if (DEBUG) {
    clearTimeout(reportTimer);
    reportTimer = setTimeout(() => api("/api/debug/perf", PERF).catch(() => {}), 300);
  }
}

const S = {
  root: null,
  recursive: true,
  platform: "",
  filter: "all",
  sort: localStorage.getItem("photto.sort") || "shuffle",
  deck: [],
  cursor: null,
  exhausted: false,
  seen: new Set(),
  filling: false,
  fillAgain: false,
  requestId: 0,
  counts: null,
  revision: 0,
  scan: null,
  truncated: false,
  loading: false,
  history: [],
  els: new Map(), // path -> card element
  muted: false,
  swipeStart: 0,
  review: { items: [], cursor: null, exhausted: false, loading: false, requestId: 0 },
};
let reviewTab = "delete";
let reviewStart = 0;
let tileObserver = null;
let reviewSentinel = null;
let scanTimer = null;

// ------------------------------------------------------------ utils -----
const fmtDate = (t) => {
  const d = new Date(t * 1000);
  const s = d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  const days = (Date.now() - d) / 864e5;
  const rel = days < 1 ? "today" : days < 2 ? "yesterday" : days < 30 ? `${Math.floor(days)} days ago`
    : days < 365 ? `${Math.floor(days / 30)} mo ago` : `${(days / 365).toFixed(1).replace(/\.0$/, "")} yr ago`;
  return `${s} · ${rel}`;
};
const fmtBytes = (n) => {
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${u[i]}`;
};
const fmtDur = (s) => {
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(x).padStart(2, "0")}`;
};
const shortPath = (p) => {
  const sep = p.includes("\\") && !p.includes("/") ? "\\" : "/";
  const t = p.replace(/^\/home\/[^/]+/, "~").replace(/^\/storage\/emulated\/0/, "Phone");
  const parts = t.split(/[\\/]/).filter(Boolean);
  return parts.length > 3 ? "…" + sep + parts.slice(-2).join(sep) : t;
};
const baseName = (p) => p.split(/[\\/]/).filter(Boolean).pop() || p;
const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

const countsTotal = (g = S.filter) => !S.counts ? 0
  : g === "all" ? (S.counts.total || 0) : (S.counts.byGroup?.[g] || 0);
const countsLeft = (g = S.filter) => !S.counts ? 0
  : g === "all" ? (S.counts.undecided || 0) : (S.counts.undecidedByGroup?.[g] || 0);
const decisionCount = (d) => S.counts?.byDecision?.[d] || 0;

// ------------------------------------------------------- index state -----
function setPlatform(pf) {
  if (!pf || S.platform === pf) return;
  S.platform = pf;
  document.body.dataset.platform = pf;
}

// Start with the cheap visual profile on Android WebViews; the server confirms the platform
// on the first response. This avoids expensive effects during the initial paint.
if (/Android/i.test(navigator.userAgent)) setPlatform("android");

function updateMeta(d) {
  if (d.platform) setPlatform(d.platform);
  if (d.counts) S.counts = d.counts;
  if (typeof d.revision === "number") S.revision = d.revision;
  if (d.scan) S.scan = d.scan;
  if (typeof d.truncated === "boolean") S.truncated = d.truncated;
  if (d.root !== undefined && d.root !== null) {
    S.root = d.root;
    S.recursive = d.recursive !== false;
    $("#folder").textContent = shortPath(d.root);
    $("#folderBtn").title = `${d.root} — click to change folder (G)`;
    $("#scopeTag").textContent = S.recursive ? "+ subfolders" : "top only";
    if (d.name) document.title = `SIFT · ${d.name}`;
  }
}

function resetDeckState() {
  S.deck = [];
  S.cursor = null;
  S.exhausted = false;
  S.seen.clear();
  S.filling = false;
  S.fillAgain = false;
  for (const el of S.els.values()) el.remove();
  S.els.clear();
  $("#deck").innerHTML = "";
}

// ------------------------------------------------------------- deck -----
async function fillDeck(first = false) {
  if (!S.root && !first) return;
  if (S.filling) { S.fillAgain = true; return; }
  S.filling = true;
  S.fillAgain = false;
  const id = S.requestId;
  try {
    let guard = 0;
    while (S.deck.length < LOW_WATER && !S.exhausted && guard++ < 3) {
      const q = new URLSearchParams({
        limit: String(PAGE), sort: S.sort, group: S.filter, decision: "none",
      });
      if (S.cursor) q.set("cursor", S.cursor);
      const d = await api(`/api/files?${q}`);
      if (id !== S.requestId) return;
      if (handleNoRoot(d)) return;
      updateMeta(d);
      const incoming = d.files || [];
      for (const f of incoming) {
        if (S.seen.has(f.path)) continue;
        S.seen.add(f.path);
        S.deck.push(f);
      }
      S.cursor = d.nextCursor || null;
      S.exhausted = !S.cursor;
      if (first && incoming.length) { first = false; if (!firstPageMarked) { firstPageMarked = true; perfValue("firstPageMs"); } }
      if (!incoming.length) break;
      if (S.deck.length >= LOW_WATER) break;
    }
  } catch (e) {
    if (id === S.requestId) toast("Couldn't load files: " + e.message);
  } finally {
    if (id === S.requestId) {
      S.filling = false;
      S.loading = false;
      render();
      updateChrome();
      if (S.deck.length < LOW_WATER && !S.exhausted && S.cursor) fillDeck();
      else if (S.fillAgain) { S.fillAgain = false; fillDeck(); }
      if (S.scan?.state === "scanning") scheduleScanPoll();
    }
  }
}

function resetDeck() {
  S.requestId++;
  resetDeckState();
  updateChrome();
  fillDeck(true);
}

function render() {
  const want = S.deck.slice(0, 3);
  const wantSet = new Set(want.map((f) => f.path));
  for (const [p, el] of S.els) {
    if (!wantSet.has(p)) { teardown(el); el.remove(); S.els.delete(p); }
  }
  want.forEach((f, i) => {
    let el = S.els.get(f.path);
    if (!el) { el = makeCard(f); S.els.set(f.path, el); $("#deck").appendChild(el); }
    el.classList.remove("top", "behind-1", "behind-2");
    el.classList.add(i === 0 ? "top" : `behind-${i}`);
    el.style.transform = "";
    if (i === 0) activate(el, f); else deactivate(el);
  });
  if (want.length && !firstCardMarked) { firstCardMarked = true; perfValue("firstCardMs"); }
  updateChrome();
}

function updateChrome() {
  const total = countsTotal();
  const left = countsLeft();
  const done = Math.max(0, total - left);
  $("#progressBar").style.width = total ? `${(done / total) * 100}%` : "0";
  if (!S.root) {
    $("#progressText").textContent = "";
  } else if (S.scan?.state === "scanning") {
    $("#progressText").textContent = `${left.toLocaleString()} ready · scanning ${(S.scan.scanned || 0).toLocaleString()} files…`;
  } else {
    $("#progressText").textContent = total
      ? `${left.toLocaleString()} left · ${done.toLocaleString()} of ${total.toLocaleString()} sorted`
      : "";
  }

  const del = decisionCount("delete");
  const b = $("#delCount");
  b.textContent = del; b.classList.toggle("zero", !del);

  // filter chips
  const nav = $("#filters");
  nav.innerHTML = "";
  for (const g of GROUPS) {
    const gTotal = countsTotal(g.id);
    const gLeft = countsLeft(g.id);
    const btn = document.createElement("button");
    btn.className = g.id === S.filter ? "on" : "";
    btn.disabled = !gTotal && g.id !== "all";
    btn.innerHTML = `${g.label}<small>${gLeft}</small>`;
    btn.onclick = () => { if (S.filter !== g.id) { S.filter = g.id; resetDeck(); } };
    nav.appendChild(btn);
  }

  const scanning = S.scan?.state === "scanning";
  const empty = !S.deck.length;
  const booting = S.loading && !S.root;
  const showSkeleton = booting || (!!S.root && empty && (scanning || S.filling) && !S.exhausted);
  $("#skeleton").classList.toggle("hidden", !showSkeleton);
  const skText = $("#skText");
  if (skText) skText.textContent = booting ? "Looking for files…" : scanning
    ? `Scanning… ${(S.scan.scanned || 0).toLocaleString()} files found`
    : "Loading files…";

  const showEmpty = !!S.root && empty && !scanning && !S.filling && !showSkeleton && !!S.counts;
  $("#empty").classList.toggle("hidden", !showEmpty);
  for (const id of ["#bDelete", "#bKeep", "#bFav", "#bSkip"]) $(id).disabled = empty;
  $("#bUndo").disabled = !S.history.length;
  $("#welcome").classList.toggle("hidden", !!S.root || booting);
  document.body.classList.toggle("no-root", !S.root);
  if (!S.root) { $("#empty").classList.add("hidden"); return; }
  if (showEmpty) {
    const nothing = !total;
    const bytes = S.counts?.bytesByDecision?.delete || 0;
    $("#emptyTitle").textContent = S.scan?.state === "error" ? "Couldn't finish reading"
      : nothing ? "Nothing here" : "All caught up";
    $("#emptyText").textContent = S.scan?.state === "error"
      ? (S.scan.error || "Some of this folder could not be read.")
      : nothing
        ? "No files of this type in this folder."
        : del ? `${del} file${del > 1 ? "s" : ""} (${fmtBytes(bytes)}) waiting in Review.`
          : "You've gone through everything in this view.";
    $("#emptyReview").classList.toggle("hidden", nothing || !del);
    $("#emptyReview").textContent = del ? "Review & clean up" : "See what you kept";
    $("#emptyRestart").classList.toggle("hidden", nothing);
  }
}

// ------------------------------------------------------------ cards -----
function makeCard(f) {
  const el = document.createElement("div");
  el.className = "card enter";
  el.style.cssText = colorVars(f.kind);
  el.innerHTML = `
    <div class="media"></div>
    <div class="meta">
      <div class="txt">
        <div class="name" title="${esc(f.path)}">${esc(f.name)}</div>
        <div class="sub">${esc(f.sizeH)} · ${esc(fmtDate(f.mtime))}${f.dir ? ` · ${esc(f.dir)}/` : ""}<span class="extra"></span></div>
      </div>
      <span class="ext">${esc(f.ext || f.kind)}</span>
    </div>
    <button class="icon-btn" title="Full view (Enter)"><svg viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg></button>
    <div class="tint"></div>
    <div class="stamp keep">KEEP</div>
    <div class="stamp del">DELETE</div>
    <div class="stamp fav">★ FAVORITE</div>`;
  el.querySelector(".icon-btn").onclick = (e) => { e.stopPropagation(); openViewer(f); };
  el.addEventListener("animationend", () => el.classList.remove("enter"), { once: true });
  setTimeout(() => el.classList.remove("enter"), 600);  // in case animations never tick (some WebViews)
  fillMedia(el, f);
  attachDrag(el, f);
  return el;
}

function glyph(f, sub, extra = "") {
  return `<div class="glyph"><div class="sheet">${esc((f.ext || f.kind).slice(0, 5))}</div>
    <div class="sub">${sub}</div>${extra}</div>`;
}

function fillMedia(el, f) {
  const m = el.querySelector(".media");
  switch (f.kind) {
    case "image":
    case "image-convert": {
      const src = thumbUrl(f, 2);
      m.innerHTML = `<div class="blur"></div><div class="loading"></div><img alt="" draggable="false" decoding="async">`;
      const img = m.querySelector("img");
      img.onload = () => { m.querySelector(".loading")?.remove(); m.querySelector(".blur").style.backgroundImage = `url("${src}")`; };
      img.onerror = () => { m.innerHTML = glyph(f, "Can't preview this image here.", openBtn()); wireOpen(m, f); };
      img.src = src;
      break;
    }
    case "video": {
      const poster = thumbUrl(f, 2);
      m.innerHTML = `<div class="blur" style="background-image:url('${poster}')"></div>
        <video playsinline loop preload="none" poster="${poster}"></video>
        <div class="badge-play hidden"></div>
        <div class="vbar"><div><i></i></div></div>`;
      const v = m.querySelector("video");
      v.onerror = () => {
        m.innerHTML = `<div class="blur" style="background-image:url('${poster}')"></div>
          <img src="${poster}" alt="" draggable="false" onerror="this.remove()">
          <div class="badge-play">can't play this format here · press O to open</div>`;
      };
      v.ontimeupdate = () => { if (v.duration) m.querySelector(".vbar i").style.width = `${(v.currentTime / v.duration) * 100}%`; };
      const bar = m.querySelector(".vbar");
      bar.onpointerdown = (e) => {
        e.stopPropagation();
        const r = bar.getBoundingClientRect();
        if (v.duration) v.currentTime = clamp((e.clientX - r.left) / r.width) * v.duration;
      };
      break;
    }
    case "audio": {
      const bars = Array.from({ length: 16 }, (_, i) => `<span style="animation-delay:${(i * 137) % 900}ms;animation-duration:${600 + ((i * 211) % 700)}ms"></span>`).join("");
      m.innerHTML = `<div class="glyph"><div class="audio-viz">${bars}</div><div class="sub">click to play / pause</div></div>
        <audio preload="none" loop></audio><div class="vbar"><div><i></i></div></div>`;
      const a = m.querySelector("audio");
      a.onplay = () => m.classList.add("playing");
      a.onpause = () => m.classList.remove("playing");
      a.ontimeupdate = () => { if (a.duration) m.querySelector(".vbar i").style.width = `${(a.currentTime / a.duration) * 100}%`; };
      const bar = m.querySelector(".vbar");
      bar.onpointerdown = (e) => { e.stopPropagation(); const r = bar.getBoundingClientRect(); if (a.duration) a.currentTime = clamp((e.clientX - r.left) / r.width) * a.duration; };
      break;
    }
    case "pdf": {
      const src = thumbUrl(f, 2);
      m.innerHTML = `<div class="loading"></div><img class="pdfthumb" alt="" draggable="false">`;
      const img = m.querySelector("img");
      img.style.background = "#fff";
      img.onload = () => m.querySelector(".loading")?.remove();
      img.onerror = () => { m.innerHTML = glyph(f, "PDF · press Enter to read it, or O to open.", openBtn()); wireOpen(m, f); };
      img.src = src;
      break;
    }
    case "text":
    case "archive": {
      m.innerHTML = `<pre>loading…</pre>`;
      el._loadText = true;
      break;
    }
    case "doc":
      m.innerHTML = glyph(f, S.platform === "android" ? "Office document · tap Open to read it." : "Office document · press Enter to preview (needs LibreOffice) or O to open.", openBtn());
      wireOpen(m, f);
      break;
    default:
      m.innerHTML = glyph(f, `${f.ext ? f.ext.toUpperCase() + " file" : "File"} · ${esc(f.sizeH)}`, openBtn());
      wireOpen(m, f);
  }
}
const openBtn = () => `<button class="btn ghost sm openx">Open with default app</button>`;
function wireOpen(m, f) { const b = m.querySelector(".openx"); if (b) b.onclick = (e) => { e.stopPropagation(); openExternal(f); }; }

async function loadInfo(el, f) {
  try {
    const info = await api(url("/info", f.path));
    const parts = [];
    if (info.resolution) parts.push(info.resolution);
    if (info.duration) parts.push(fmtDur(info.duration));
    if (info.artist || info.title) parts.push([info.artist, info.title].filter(Boolean).join(" – "));
    if (parts.length) el.querySelector(".extra").textContent = " · " + parts.join(" · ");
  } catch { /* optional */ }
}

async function loadText(el, f) {
  if (!el._loadText) return;
  el._loadText = false;
  const pre = el.querySelector("pre");
  try {
    const d = await api(url("/text", f.path));
    pre.textContent = (d.text || "(empty file)").slice(0, 20000) + (d.truncated || d.text.length > 20000 ? "\n\n…" : "");
    if (f.kind === "archive" && !d.text) pre.textContent = "(can't list this archive — press O to open it)";
  } catch { pre.textContent = "(couldn't read file)"; }
}

function playerOf(el) { return el.querySelector("video, audio"); }

function activate(el, f) {
  loadText(el, f);
  const p = playerOf(el);
  if (p) {
    if (!p.getAttribute("src")) {
      p.src = url("/raw", f.path);
      if (p.tagName === "VIDEO") el._infoLoaded || (el._infoLoaded = true, loadInfo(el, f));
      else el._infoLoaded || (el._infoLoaded = true, loadInfo(el, f));
    }
    p.muted = S.muted;
    p.play().catch(() => {
      // autoplay with sound blocked until the user interacts: retry muted for video
      if (p.tagName === "VIDEO") { p.muted = true; p.play().catch(() => {}); }
    });
  }
}
function deactivate(el) { const p = playerOf(el); if (p && !p.paused) p.pause(); }
function teardown(el) {
  const p = playerOf(el);
  if (p) { p.pause(); p.removeAttribute("src"); p.load(); }
  el.querySelectorAll("img").forEach((img) => img.removeAttribute("src"));
}

// ------------------------------------------------------------- drag -----
const TH = 110;
function attachDrag(el, f) {
  let sx = 0, sy = 0, dx = 0, dy = 0, t0 = 0, active = false, moved = false, raf = 0;
  const stamps = { keep: el.querySelector(".stamp.keep"), del: el.querySelector(".stamp.del"), fav: el.querySelector(".stamp.fav") };
  const tint = el.querySelector(".tint");

  const paint = () => {
    raf = 0;
    el.style.transform = `translate3d(${dx}px, ${dy}px, 0) rotate(${dx / 16}deg)`;
    paintStamps(stamps, tint, dx, dy);
  };

  el.addEventListener("pointerdown", (e) => {
    if (!el.classList.contains("top") || e.button !== 0 || e.target.closest("button, .vbar")) return;
    active = true; moved = false; sx = e.clientX; sy = e.clientY; dx = dy = 0; t0 = performance.now();
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener("pointermove", (e) => {
    if (!active) return;
    dx = e.clientX - sx; dy = e.clientY - sy;
    if (!moved && Math.hypot(dx, dy) > 6) {
      moved = true; el.classList.add("dragging");
    }
    if (!moved) return;
    if (!raf) raf = requestAnimationFrame(paint);
  });
  const end = (e) => {
    if (!active) return;
    active = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    el.classList.remove("dragging");
    // touch screens deliver few pointermoves on a quick flick: use where the finger lifted
    if (e.type === "pointerup") { dx = e.clientX - sx; dy = e.clientY - sy; if (Math.hypot(dx, dy) > 6) moved = true; }
    if (!moved) { onTap(el); return; }
    // "Swipe to next card" is the app's response after the finger lifts, not the
    // user's gesture duration. Start timing at release so the budget measures jank.
    if (e.type === "pointerup") S.swipeStart = performance.now();
    const vertical = Math.abs(dy) > Math.abs(dx);
    const fast = Math.hypot(dx, dy) / Math.max(1, performance.now() - t0) > 0.6;  // px/ms
    const th = fast ? TH * 0.45 : TH;
    if (dx > th && !vertical) decide("keep", dx, dy);
    else if (dx < -th && !vertical) decide("delete", dx, dy);
    else if (dy < -th && vertical) decide("favorite", dx, dy);
    else if (dy > th * 1.3 && vertical) skip();
    else { el.style.transform = ""; paintStamps(stamps, tint, 0, 0); S.swipeStart = 0; }
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("dblclick", () => openViewer(f));
}

function paintStamps(st, tint, dx, dy) {
  const vertical = Math.abs(dy) > Math.abs(dx);
  const k = vertical ? 0 : clamp(dx / TH), d = vertical ? 0 : clamp(-dx / TH), v = vertical ? clamp(-dy / TH) : 0;
  st.keep.style.opacity = k; st.del.style.opacity = d; st.fav.style.opacity = v;
  const c = k > 0 ? "126,224,161" : d > 0 ? "255,122,107" : "255,207,92";
  tint.style.background = `rgba(${c},.18)`;
  tint.style.opacity = Math.max(k, d, v);
}

function onTap(el) {
  const p = playerOf(el);
  if (p) {
    if (p.paused) { p.muted = S.muted; p.play().catch(() => {}); } else p.pause();
  }
}

// -------------------------------------------------------- decisions -----
function flyOut(el, decision, dx = 0, dy = 0) {
  const w = window.innerWidth;
  const st = el.querySelector(decision === "keep" ? ".stamp.keep" : decision === "delete" ? ".stamp.del" : ".stamp.fav");
  if (st) st.style.opacity = 1;
  el.style.transition = REDUCED ? "none" : "transform .4s cubic-bezier(.3,.6,.4,1), opacity .4s";
  const tx = decision === "keep" ? w : decision === "delete" ? -w : dx;
  const ty = decision === "favorite" ? -window.innerHeight : decision === "skip" ? window.innerHeight : dy;
  el.style.transform = `translate3d(${tx}px, ${ty}px, 0) rotate(${decision === "keep" ? 28 : decision === "delete" ? -28 : 0}deg)`;
  el.style.opacity = "0";
  el.classList.remove("top");
  el.classList.add("flying");
  setTimeout(() => { teardown(el); el.remove(); el.classList.remove("flying"); }, REDUCED ? 20 : 420);
}

function adjustCounts(f, newDecision, oldDecision) {
  const c = S.counts;
  if (!c) return;
  newDecision = newDecision || null;
  oldDecision = oldDecision || null;
  if (newDecision === oldDecision) return;
  const size = f.size || 0;
  const byD = c.byDecision || (c.byDecision = { keep: 0, delete: 0, favorite: 0 });
  const byB = c.bytesByDecision || (c.bytesByDecision = { keep: 0, delete: 0, favorite: 0 });
  if (oldDecision) {
    byD[oldDecision] = Math.max(0, (byD[oldDecision] || 0) - 1);
    byB[oldDecision] = Math.max(0, (byB[oldDecision] || 0) - size);
  }
  if (newDecision) {
    byD[newDecision] = (byD[newDecision] || 0) + 1;
    byB[newDecision] = (byB[newDecision] || 0) + size;
  }
  const change = (oldDecision ? 1 : 0) - (newDecision ? 1 : 0);
  c.undecided = Math.max(0, (c.undecided || 0) + change);
  const g = groupOf(f.kind);
  if (c.undecidedByGroup && c.undecidedByGroup[g] !== undefined) {
    c.undecidedByGroup[g] = Math.max(0, c.undecidedByGroup[g] + change);
  }
}

function decide(decision, dx = 0, dy = 0) {
  const f = S.deck.shift();
  if (!f) return;
  const el = S.els.get(f.path);
  S.els.delete(f.path);
  if (el) { deactivate(el); flyOut(el, decision, dx, dy); }
  S.history.push({ f, prev: f.decision || null, type: "decide" });
  adjustCounts(f, decision, f.decision || null);
  f.decision = decision;
  api("/api/decide", { path: f.path, decision }).catch(() => {
    toast("Couldn't save — is SIFT still running?");
    load();
  });
  if (decision === "delete") { const b = $("#reviewBtn"); b.classList.remove("bump"); void b.offsetWidth; b.classList.add("bump"); }
  render();
  if (S.swipeStart) {
    const ms = performance.now() - S.swipeStart;
    perfValue("swipeToNextMs", PERF.swipeToNextMs == null ? ms : Math.max(PERF.swipeToNextMs, ms));
    S.swipeStart = 0;
  }
  if (S.deck.length < LOW_WATER) fillDeck();
}

function skip() {
  if (S.deck.length < 2) {
    const el = S.els.get(S.deck[0]?.path); if (el) el.style.transform = "";
    if (!S.exhausted && S.cursor) { fillDeck(); toast("Loading more…"); }
    else toast("This is the last one");
    return;
  }
  const f = S.deck.shift();
  const el = S.els.get(f.path);
  S.els.delete(f.path);
  if (el) { deactivate(el); flyOut(el, "skip"); }
  S.deck.push(f);
  S.history.push({ f, type: "skip" });
  render();
}

function undo() {
  const h = S.history.pop();
  if (!h) { toast("Nothing to undo"); return; }
  if (h.type === "skip") {
    const i = S.deck.indexOf(h.f);
    if (i >= 0) S.deck.splice(i, 1);
  } else {
    adjustCounts(h.f, h.prev || null, h.f.decision || null);
    h.f.decision = h.prev || null;
    api("/api/decide", { path: h.f.path, decision: h.prev || null }).catch(() => {});
  }
  if (inGroup(h.f)) S.deck.unshift(h.f); else toast(`Restored ${h.f.name} (in another filter)`);
  render();
}

async function refreshCounts() {
  try { const d = await api("/api/scan"); updateMeta(d); updateChrome(); } catch { /* ignore */ }
}

async function openExternal(f, folder = false) {
  try { await api("/api/open", { path: f.path, folder }); toast(folder ? "Opened folder" : `Opening ${f.name}…`); }
  catch { toast("Couldn't open (is xdg-open installed?)"); }
}

// ----------------------------------------------------------- viewer -----
let viewerFile = null;
function openViewer(f) {
  if (!f) return;
  viewerFile = f;
  const el = S.els.get(f.path); if (el) deactivate(el);
  $("#viewerName").textContent = f.path;
  const body = $("#viewerBody");
  const raw = url("/raw", f.path), view = url("/view", f.path);
  if (f.kind === "image" || f.kind === "image-convert") body.innerHTML = `<img src="${view}" alt="">`;
  else if (f.kind === "video") body.innerHTML = `<video src="${raw}" controls autoplay></video>`;
  else if (f.kind === "audio") body.innerHTML = `<audio src="${raw}" controls autoplay></audio>`;
  else if (f.kind === "pdf" || f.kind === "doc") {
    body.innerHTML = `<div class="loading" style="position:static"></div>`;
    api(url("/pdfinfo", f.path)).then((d) => {
      if (viewerFile !== f) return;
      if (d.pages > 0) {
        const n = Math.min(d.pages, 500);
        body.innerHTML = `<div class="pages">${Array.from({ length: n }, (_, i) =>
          `<img loading="lazy" src="${url("/pdfpage", f.path)}&n=${i + 1}" alt="page ${i + 1}">`).join("")}
          ${d.pages > n ? `<p class="muted">… ${d.pages - n} more pages — press O to open the whole file</p>` : ""}</div>`;
      } else if (f.kind === "pdf") {
        body.innerHTML = `<iframe src="${raw}"></iframe>`;
      } else {
        body.innerHTML = glyph(f, S.platform === "android" ? "No preview here. Use \u201cOpen in app\u201d to read it." : "Install LibreOffice to preview Office documents here.", openBtn());
        wireOpen(body, f);
      }
    }).catch(() => { body.innerHTML = glyph(f, "Couldn't preview this file.", openBtn()); wireOpen(body, f); });
  } else if (f.kind === "text" || f.kind === "archive") {
    body.innerHTML = `<pre>loading…</pre>`;
    api(url("/text", f.path)).then((d) => { body.querySelector("pre").textContent = d.text + (d.truncated ? "\n\n… (truncated)" : ""); });
  } else body.innerHTML = glyph(f, "No preview for this file type.", openBtn()), wireOpen(body, f);
  $("#viewer").classList.remove("hidden");
}
function closeViewer() {
  $("#viewer").classList.add("hidden");
  const b = $("#viewerBody"); b.querySelectorAll("video,audio").forEach((m) => m.pause()); b.innerHTML = "";
  viewerFile = null;
  const top = S.deck[0]; if (top && S.els.get(top.path)) activate(S.els.get(top.path), top);
}

// ----------------------------------------------------------- review -----
function ensureTileObserver() {
  if (tileObserver) tileObserver.disconnect();
  tileObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const img = e.target.querySelector("img[data-src]");
      if (img) { img.src = img.dataset.src; delete img.dataset.src; }
      tileObserver.unobserve(e.target);
    }
  }, { root: $("#reviewGrid"), rootMargin: "200px 0px" });
}

function updateReviewTabs() {
  $("#tDel").textContent = decisionCount("delete");
  $("#tFav").textContent = decisionCount("favorite");
  $("#tKeep").textContent = decisionCount("keep");
  document.querySelectorAll("#reviewTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === reviewTab));
}

function updateReviewFooter() {
  const del = decisionCount("delete");
  const fav = decisionCount("favorite");
  const bytes = S.counts?.bytesByDecision?.delete || 0;
  $("#freeText").textContent = del ? `frees ${fmtBytes(bytes)}` : "";
  const favMove = $("#favMove").checked && fav;
  const btn = $("#commitBtn");
  btn.disabled = !del && !favMove;
  btn.dataset.armed = "";
  btn.textContent = del ? `Move ${del} file${del > 1 ? "s" : ""} to Trash` : favMove ? "Move favorites" : "Move to Trash";
}

function reviewEmptyText() {
  return {
    delete: "Nothing marked for deletion.",
    favorite: "No favorites yet — swipe up on something you love.",
    keep: "Nothing kept yet.",
  }[reviewTab];
}

function appendReviewTiles(files) {
  const grid = $("#reviewGrid");
  const fragment = document.createDocumentFragment();
  const label = { delete: "Rescue", favorite: "Unfavorite", keep: "Un-keep" }[reviewTab];
  for (const f of files) {
    S.review.items.push(f);
    const t = document.createElement("div");
    t.className = "tile";
    t.style.cssText = colorVars(f.kind);
    const thumb = ["image", "image-convert", "video", "pdf"].includes(f.kind);
    t.innerHTML = `${thumb ? `<img loading="lazy" alt="">` : ""}
      <div class="tg" ${thumb ? 'style="z-index:-1"' : ""}>${esc(f.ext || f.kind)}</div>
      <div class="cap">${esc(f.name)}<small>${esc(f.sizeH)}</small></div>
      <div class="undo">↩ ${label} — put back in the deck</div>`;
    const img = t.querySelector("img");
    if (img) { img.dataset.src = thumbUrl(f, 2); tileObserver.observe(t); }
    t.onclick = () => undoReview(f, t);
    fragment.appendChild(t);
  }
  grid.appendChild(fragment);
  ensureReviewSentinel();
}

function ensureReviewSentinel() {
  const grid = $("#reviewGrid");
  if (!reviewSentinel) {
    reviewSentinel = document.createElement("div");
    reviewSentinel.id = "reviewSentinel";
    reviewSentinel.className = "sentinel";
  }
  grid.appendChild(reviewSentinel);
}

async function loadReviewPage(initial = false) {
  const r = S.review;
  if (r.loading || r.exhausted) return;
  const id = r.requestId;
  r.loading = true;
  try {
    const q = new URLSearchParams({ limit: String(REVIEW_PAGE), sort: "largest", decision: reviewTab });
    if (r.cursor) q.set("cursor", r.cursor);
    const d = await api(`/api/files?${q}`);
    if (id !== r.requestId) return;
    updateMeta(d);
    const incoming = d.files || [];
    r.cursor = d.nextCursor || null;
    r.exhausted = !r.cursor;
    if (incoming.length) appendReviewTiles(incoming);
    else if (initial) {
      const grid = $("#reviewGrid");
      grid.innerHTML = `<div class="none">${reviewEmptyText()}</div>`;
    }
    if (initial) perfValue("reviewOpenMs", performance.now() - reviewStart);
    updateReviewTabs();
    updateReviewFooter();
    if (!r.exhausted) requestAnimationFrame(() => {
      const grid = $("#reviewGrid");
      if (grid.scrollHeight <= grid.clientHeight + 40) loadReviewPage();
    });
  } catch (e) {
    if (id === r.requestId) toast("Couldn't load Review: " + e.message);
  } finally {
    if (id === r.requestId) r.loading = false;
  }
}

function resetReview() {
  const r = S.review;
  r.requestId++;
  r.items = [];
  r.cursor = null;
  r.exhausted = false;
  r.loading = false;
  reviewSentinel = null;
  const grid = $("#reviewGrid");
  grid.innerHTML = "";
  grid.scrollTop = 0;
  ensureTileObserver();
  updateReviewTabs();
  updateReviewFooter();
  loadReviewPage(true);
}

function drawReview() { resetReview(); }

function openReview() {
  if (!S.root) return;
  const top = S.deck[0]; if (top && S.els.get(top.path)) deactivate(S.els.get(top.path));
  if (!decisionCount("delete")) reviewTab = decisionCount("favorite") ? "favorite" : decisionCount("keep") ? "keep" : "delete";
  $("#review").classList.remove("hidden");
  reviewStart = performance.now();
  resetReview();
}

function closeReview() {
  $("#review").classList.add("hidden");
  S.review.requestId++;
  if (tileObserver) { tileObserver.disconnect(); tileObserver = null; }
  reviewSentinel = null;
  $("#reviewGrid").innerHTML = "";
  const top = S.deck[0]; if (top && S.els.get(top.path)) activate(S.els.get(top.path), top);
}

function undoReview(f, tile) {
  const old = f.decision || reviewTab;
  f.decision = null;
  adjustCounts(f, null, old);
  api("/api/decide", { path: f.path, decision: null }).catch(() => {
    toast("Couldn't undo that mark");
    load();
  });
  tile.classList.add("gone");
  setTimeout(() => {
    tile.remove();
    const grid = $("#reviewGrid");
    if (!grid.querySelector(".tile") && !S.review.cursor) {
      grid.innerHTML = `<div class="none">${reviewEmptyText()}</div>`;
    }
  }, 220);
  if (inGroup(f) && !S.deck.some((x) => x.path === f.path)) S.deck.unshift(f);
  updateReviewTabs();
  updateReviewFooter();
  render();
}

async function commit() {
  const btn = $("#commitBtn");
  if (!btn.dataset.armed) {
    btn.dataset.armed = "1";
    const orig = btn.textContent;
    btn.textContent = "Click again to confirm";
    setTimeout(() => { if (btn.dataset.armed) { btn.dataset.armed = ""; btn.textContent = orig; } }, 3000);
    return;
  }
  btn.disabled = true; btn.textContent = "Working…";
  try {
    const r = await api("/api/commit", { favoritesFolder: $("#favMove").checked ? "Favorites" : null });
    const bits = [];
    if (r.trashed.length) bits.push(`Moved ${r.trashed.length} to Trash · freed ${r.freedH}`);
    if (r.favorited.length) bits.push(`${r.favorited.length} favorites → Favorites/`);
    if (r.failed.length) bits.push(`${r.failed.length} failed`);
    toast(bits.join(" · ") || "Done");
    if (r.failed.length) console.warn("SIFT: failed", r.failed);
    S.history = [];
    await load();
    resetReview();
  } catch (e) {
    toast("Something went wrong: " + e.message);
    resetReview();
  }
}

// ----------------------------------------------------------- picker -----
let pickerPath = null;
async function openPicker() {
  const top = S.deck[0]; if (top && S.els.get(top.path)) deactivate(S.els.get(top.path));
  $("#recursiveBox").checked = S.recursive !== false;
  $("#picker").classList.remove("hidden");
  await browse(S.root || "");
}
function closePicker() {
  $("#picker").classList.add("hidden");
  const top = S.deck[0]; if (top && S.els.get(top.path)) activate(S.els.get(top.path), top);
}
async function browse(path) {
  let d;
  try { d = await api(`/api/dirs?path=${encodeURIComponent(path || "")}`); }
  catch { toast("Can't open that folder"); return; }
  pickerPath = d.path;
  $("#pathInput").value = d.path;
  $("#upBtn").disabled = !d.parent;
  $("#upBtn").onclick = () => d.parent && browse(d.parent);
  const places = $("#places"); places.innerHTML = "";
  const section = (title, items) => {
    if (!items?.length) return;
    const h = document.createElement("h4"); h.textContent = title; places.appendChild(h);
    for (const pl of items) {
      const b = document.createElement("button");
      b.textContent = pl.name; b.title = pl.path;
      b.className = d.path === pl.path ? "on" : "";
      b.onclick = () => browse(pl.path);
      b.ondblclick = () => chooseFolder(pl.path);
      places.appendChild(b);
    }
  };
  section("Recent", d.recent);
  section("Places", d.places);
  const list = $("#dirList"); list.innerHTML = "";
  if (d.error) list.innerHTML = `<div class="none">${esc(d.error)}</div>`;
  else if (!d.dirs.length) list.innerHTML = `<div class="none">No subfolders here — you can open this one.</div>`;
  for (const dir of d.dirs) {
    const b = document.createElement("button");
    b.textContent = dir.name; b.title = dir.path;
    b.onclick = () => browse(dir.path);
    b.ondblclick = () => chooseFolder(dir.path);
    list.appendChild(b);
  }
}
async function chooseFolder(path) {
  try {
    await api("/api/root", { path, recursive: $("#recursiveBox").checked });
  } catch { toast("Couldn't open that folder"); return; }
  $("#picker").classList.add("hidden");
  S.history = [];
  await load();
  toast(`Now sorting ${shortPath(S.root)} · ${countsTotal("all").toLocaleString()} files`);
}
$("#folderBtn").onclick = openPicker;
let textJobId = null, textPoll = null, textOffset = 0;
const textSelected = new Set();
function syncTextActions() {
  const disabled = textSelected.size === 0;
  $("#textCopy").disabled = disabled;
  $("#textMove").disabled = disabled;
}
function textUrl(path, p) { return `${path}?p=${encodeURIComponent(p)}&t=${encodeURIComponent(TOKEN)}`; }
function closeTextFinder() { $("#textFinder").classList.add("hidden"); }
$("#findTextBtn").onclick = () => { if (!S.root) return openPicker(); $("#textFinder").classList.remove("hidden"); };
$("#textClose").onclick = closeTextFinder;
$("#textFinder").onclick = (e) => { if (e.target.id === "textFinder") closeTextFinder(); };
$("#textForm").onsubmit = async (e) => {
  e.preventDefault(); if (!S.root) return;
  clearInterval(textPoll); textOffset = 0; textSelected.clear(); syncTextActions(); $("#textResults").replaceChildren();
  try {
    const r = await api("/api/text/start", {language: $("#textLanguage").value || "eng", threshold: Number($("#textThreshold").value || 2), query: $("#textQuery").value, images: $("#textImages").checked, videos: $("#textVideos").checked});
    textJobId = r.jobId; $("#textCancel").classList.remove("hidden"); $("#textStatus").textContent = `Scanning ${r.total} files…`;
    textPoll = setInterval(updateTextJob, 700); updateTextJob();
  } catch (err) { $("#textStatus").textContent = err.message; }
};
async function updateTextJob() {
  if (!textJobId) return;
  try {
    const s = await api(`/api/text/status?jobId=${encodeURIComponent(textJobId)}`);
    const problem = s.errors.length ? ` · ${s.errors[0].error}` : "";
    $("#textStatus").textContent = `${s.state}: ${s.done}/${s.total} checked · ${s.matches} matches · ${s.errors.length} errors${problem}`;
    if (s.state !== "running") { clearInterval(textPoll); $("#textCancel").classList.add("hidden"); }
    if (s.errors.length) $("#textStatus").title = s.errors.map((x) => x.error).join("\n");
    await loadTextResults(false, s.state);
  } catch { clearInterval(textPoll); }
}
async function loadTextResults(more, state = "") {
  const r = await api(`/api/text/results?jobId=${encodeURIComponent(textJobId)}&offset=${textOffset}&limit=40`);
  if (r.items.length) $("#textResults .text-empty")?.remove();
  for (const item of r.items) {
    const row = document.createElement("label"); row.className = "text-row";
    const check = document.createElement("input"); check.type = "checkbox"; check.checked = textSelected.has(item.path);
    check.onchange = () => { check.checked ? textSelected.add(item.path) : textSelected.delete(item.path); syncTextActions(); };
    const img = document.createElement("img"); img.loading = "lazy"; img.src = textUrl("/thumb", item.path);
    const content = document.createElement("div");
    content.innerHTML = `<strong>${esc(item.path)}${item.timestamp == null ? "" : ` · ${item.timestamp}s`}</strong><p>${esc(item.text.slice(0, 300))}</p><small>${esc(item.reason)} match</small>`;
    row.append(check, img, content); $("#textResults").append(row);
  }
  if (!$("#textResults .text-row")) {
    const empty = document.createElement("p"); empty.className = "text-empty";
    empty.textContent = state === "running" ? "Scanning locally. Matches will appear here as they are found." : textJobId ? "No matches found. Try a broader query or lower the minimum word count." : "Choose a scan scope, then search locally on this device. Matches will appear here.";
    $("#textResults").replaceChildren(empty);
  }
  textOffset = r.offset + r.items.length; $("#textMore").classList.toggle("hidden", r.nextOffset == null);
}
$("#textMore").onclick = () => loadTextResults(true);
$("#textCancel").onclick = async () => { if (textJobId) await api("/api/text/cancel", {jobId: textJobId}); };
$("#textClear").onclick = async () => { if (confirm("Clear cached OCR text? Review marks will be kept.")) { await api("/api/text/clear", {}); toast("OCR cache cleared"); } };
async function textAction(action) {
  if (!textJobId || !textSelected.size) return toast("Select one or more matches first");
  const paths = [...textSelected];
  const preview = await api("/api/text/preview", {jobId: textJobId, paths});
  const lines = preview.destinations.map((x) => `${x.path} → ${x.destination}`).join("\n");
  if (!confirm(`${action === "copy" ? "Copy" : "Move"} ${paths.length} file(s) to SIFT_docs?\n\n${lines}`)) return;
  const out = await api("/api/text/action", {jobId: textJobId, paths, action});
  toast(`${out.copied} copied · ${out.moved} moved · ${out.failed} failed`);
}
$("#textCopy").onclick = () => textAction("copy").catch((e) => toast(e.message));
$("#textMove").onclick = () => textAction("move").catch((e) => toast(e.message));
$("#emptyPick").onclick = openPicker;
$("#pickerClose").onclick = closePicker;
$("#picker").onclick = (e) => { if (e.target.id === "picker") closePicker(); };
$("#pathForm").onsubmit = (e) => { e.preventDefault(); browse($("#pathInput").value.trim()); };
$("#pickBtn").onclick = () => pickerPath && chooseFolder(pickerPath);
$("#nativePick").onclick = async () => {
  const r = await api("/api/pick", { start: pickerPath || S.root }).catch(() => ({ error: "x" }));
  if (r.error) toast("No system folder dialog found (install zenity or kdialog)");
  else if (r.path) chooseFolder(r.path);
};

// ------------------------------------------------------------- load -----
function handleNoRoot(d) {
  if (d.needsPermission) {
    resetDeckState();
    S.root = null; S.counts = null; S.scan = null;
    $("#welcomeTitle").textContent = "Allow access to your files";
    $("#welcomeText").textContent = "SIFT needs \u201cAll files access\u201d to show and organize your folders. Nothing leaves your phone.";
    $("#welcomePick").textContent = "Allow access";
    $("#welcomePick").onclick = () => api("/api/permission", {});
    updateChrome();
    return true;
  }
  if (d.needsPick) {
    resetDeckState();
    S.root = null; S.counts = null; S.scan = null;
    $("#welcomeTitle").textContent = "Pick a folder to tidy";
    $("#welcomeText").textContent = "Swipe through every photo, video, song and document in it \u2014 keep, favorite or trash.";
    $("#welcomePick").textContent = "Choose folder…";
    $("#welcomePick").onclick = openPicker;
    $("#folder").textContent = "Pick a folder";
    $("#scopeTag").textContent = "";
    renderWelcome(d.recent || []);
    updateChrome();
    openPicker();
    return true;
  }
  return false;
}

async function load() {
  S.requestId++;
  resetDeckState();
  S.history = [];
  S.counts = null;
  S.scan = null;
  S.root = null;
  S.loading = true;
  updateChrome();
  await fillDeck(true);
  if (S.scan?.state === "scanning") scheduleScanPoll();
}

function scheduleScanPoll() {
  clearTimeout(scanTimer);
  if (!S.root || S.scan?.state !== "scanning") return;
  scanTimer = setTimeout(pollScan, 700);
}

async function pollScan() {
  if (!S.root) return;
  try {
    const wasScanning = S.scan?.state === "scanning";
    const d = await api("/api/scan");
    updateMeta(d);
    updateChrome();
    if (S.scan?.state === "scanning") {
      if (S.deck.length < LOW_WATER) fillDeck();
      scheduleScanPoll();
    } else if (wasScanning) {
      await reconcileAfterScan();
    }
  } catch {
    scanTimer = setTimeout(pollScan, 1500);
  }
}

async function reconcileAfterScan() {
  const top = S.deck[0] || null;
  // Preserve the top card only while the file still exists; a revalidation may
  // have removed it, and keeping it would leave a card that can never be decided.
  let keepTop = false;
  if (top && !top.decision && inGroup(top)) {
    try { await api(url("/info", top.path)); keepTop = true; } catch { keepTop = false; }
  }
  S.requestId++;
  S.deck = [];
  S.cursor = null;
  S.exhausted = false;
  S.seen.clear();
  S.filling = false;
  S.fillAgain = false;
  for (const el of S.els.values()) el.remove();
  S.els.clear();
  $("#deck").innerHTML = "";
  if (keepTop) {
    S.deck.push(top);
    S.seen.add(top.path);
  }
  await fillDeck(true);
  updateChrome();
}

function setPlatformReset() { /* kept for symmetry with older clients */ }

window.phottoBack = () => {
  for (const [id, close] of [["#viewer", closeViewer], ["#picker", closePicker], ["#review", closeReview]]) {
    if (!$(id).classList.contains("hidden")) { close(); return true; }
  }
  return false;
};

function renderWelcome(recent) {
  const box = $("#welcomeRecent"); box.innerHTML = "";
  for (const r of recent.slice(0, 5)) {
    const b = document.createElement("button");
    b.className = "recent-item";
    b.innerHTML = `<b>${esc(baseName(r))}</b><small>${esc(shortPath(r))}</small>`;
    b.onclick = () => chooseFolder(r);
    box.appendChild(b);
  }
}

// ------------------------------------------------------------ wiring -----
$("#bDelete").onclick = () => decide("delete");
$("#bKeep").onclick = () => decide("keep");
$("#bFav").onclick = () => decide("favorite");
$("#bSkip").onclick = () => skip();
$("#bUndo").onclick = () => undo();
$("#reviewBtn").onclick = openReview;
$("#emptyReview").onclick = openReview;
$("#emptyRestart").onclick = async () => {
  if (!confirm("Forget all your keep/favorite/delete marks for this folder and start again? (No files are touched.)")) return;
  await api("/api/reset", {});
  S.history = [];
  await load();
};
$("#reviewClose").onclick = closeReview;
$("#review").onclick = (e) => { if (e.target.id === "review") closeReview(); };
$("#reviewGrid").addEventListener("scroll", () => {
  if ($("#review").classList.contains("hidden")) return;
  const g = $("#reviewGrid");
  if (g.scrollHeight - g.scrollTop - g.clientHeight < 600) loadReviewPage();
}, { passive: true });
$("#reviewTabs").onclick = (e) => {
  const b = e.target.closest("button");
  if (b && b.dataset.tab !== reviewTab) { reviewTab = b.dataset.tab; resetReview(); }
};
$("#favMove").onchange = updateReviewFooter;
$("#commitBtn").onclick = commit;
$("#viewerClose").onclick = closeViewer;
$("#viewerOpen").onclick = () => viewerFile && openExternal(viewerFile);
$("#viewerFolder").onclick = () => viewerFile && openExternal(viewerFile, true);
const sortSel = $("#sort");
sortSel.value = S.sort;
sortSel.onchange = () => {
  S.sort = sortSel.value; localStorage.setItem("photto.sort", S.sort);
  resetDeck(); sortSel.blur();
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#picker").classList.contains("hidden")) { closePicker(); return; }
  if (!$("#picker").classList.contains("hidden")) return;
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.ctrlKey && e.key !== "z" || e.altKey || e.metaKey) return;
  const reviewOpen = !$("#review").classList.contains("hidden");
  const viewerOpen = !$("#viewer").classList.contains("hidden");
  if (e.key === "Escape") { if (viewerOpen) closeViewer(); else if (reviewOpen) closeReview(); return; }
  if (reviewOpen) return;
  const k = e.key.toLowerCase();
  const act = (fn) => { e.preventDefault(); if (viewerOpen) closeViewer(); fn(); };
  if (e.key === "ArrowLeft" || k === "x" || k === "delete") act(() => decide("delete"));
  else if (e.key === "ArrowRight" || k === "k") act(() => decide("keep"));
  else if (e.key === "ArrowUp" || k === "f") act(() => decide("favorite"));
  else if (e.key === "ArrowDown" || k === "s") act(() => skip());
  else if (k === "z" || e.key === "Backspace") act(undo);
  else if (k === "r") act(openReview);
  else if (k === "g") act(openPicker);
  else if (e.key === "Enter") { e.preventDefault(); viewerOpen ? closeViewer() : openViewer(S.deck[0]); }
  else if (k === "o" && S.deck[0]) { e.preventDefault(); openExternal(viewerFile || S.deck[0]); }
  else if (k === "m") {
    e.preventDefault(); S.muted = !S.muted;
    document.querySelectorAll("video,audio").forEach((p) => (p.muted = S.muted));
    toast(S.muted ? "Muted" : "Sound on");
  } else if (e.key === " " && !viewerOpen && S.deck[0]) {
    e.preventDefault(); const el = S.els.get(S.deck[0].path); if (el) onTap(el);
  }
});

if (!TOKEN) {
  document.body.innerHTML = `<div style="margin:auto;text-align:center;font-family:sans-serif;color:#aaa">
    Missing access token. Open the full URL printed by the SIFT launcher.</div>`;
} else {
  load().catch((e) => toast("Couldn't load files: " + e.message));
  // heartbeat: lets SIFT quit by itself once this window is closed
  let lost = 0;
  setInterval(() => {
    fetch("/api/ping", { headers: { "X-Token": TOKEN } })
      .then(() => { if (lost) { lost = 0; $("#gone").classList.add("hidden"); } })
      .catch(() => { if (++lost >= 2) $("#gone").classList.remove("hidden"); });
  }, 5000);
}
$("#welcomePick").onclick = openPicker;
