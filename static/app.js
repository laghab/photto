"use strict";

// ------------------------------------------------------------ setup -----
const TOKEN = new URLSearchParams(location.hash.slice(1)).get("t") || "";
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

const S = {
  files: [],
  filter: "all",
  sort: localStorage.getItem("photto.sort") || "shuffle",
  deck: [],
  history: [],
  els: new Map(), // path -> card element
  muted: false,
  shuffleKey: new Map(),
};

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
const inGroup = (f, g = S.filter) => { const k = GROUPS.find((x) => x.id === g).kinds; return !k || k.includes(f.kind); };

// ------------------------------------------------------------- deck -----
function buildDeck() {
  const list = S.files.filter((f) => !f.decision && inGroup(f));
  const by = {
    shuffle: (a, b) => S.shuffleKey.get(a.path) - S.shuffleKey.get(b.path),
    newest: (a, b) => b.mtime - a.mtime,
    oldest: (a, b) => a.mtime - b.mtime,
    largest: (a, b) => b.size - a.size,
    name: (a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }),
  }[S.sort];
  S.deck = list.sort(by);
  for (const el of S.els.values()) el.remove();
  S.els.clear();
  render();
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
  updateChrome();
}

function updateChrome() {
  const scope = S.files.filter((f) => inGroup(f));
  const done = scope.filter((f) => f.decision).length;
  $("#progressBar").style.width = scope.length ? `${(done / scope.length) * 100}%` : "0";
  $("#progressText").textContent = scope.length
    ? `${S.deck.length.toLocaleString()} left · ${done.toLocaleString()} of ${scope.length.toLocaleString()} sorted`
    : "";

  const del = S.files.filter((f) => f.decision === "delete");
  const b = $("#delCount");
  b.textContent = del.length; b.classList.toggle("zero", !del.length);

  // filter chips
  const nav = $("#filters");
  nav.innerHTML = "";
  for (const g of GROUPS) {
    const total = S.files.filter((f) => inGroup(f, g.id)).length;
    const left = S.files.filter((f) => inGroup(f, g.id) && !f.decision).length;
    const btn = document.createElement("button");
    btn.className = g.id === S.filter ? "on" : "";
    btn.disabled = !total && g.id !== "all";
    btn.innerHTML = `${g.label}<small>${left}</small>`;
    btn.onclick = () => { S.filter = g.id; buildDeck(); };
    nav.appendChild(btn);
  }

  const empty = !S.deck.length;
  $("#empty").classList.toggle("hidden", !empty);
  for (const id of ["#bDelete", "#bKeep", "#bFav", "#bSkip"]) $(id).disabled = empty;
  $("#bUndo").disabled = !S.history.length;
  $("#welcome").classList.toggle("hidden", !!S.root);
  document.body.classList.toggle("no-root", !S.root);
  if (!S.root) { $("#empty").classList.add("hidden"); return; }
  if (empty) {
    const nothing = !scope.length;
    $("#emptyTitle").textContent = nothing ? "Nothing here" : "All caught up";
    $("#emptyText").textContent = nothing
      ? "No files of this type in this folder."
      : del.length ? `${del.length} file${del.length > 1 ? "s" : ""} (${fmtBytes(del.reduce((a, f) => a + f.size, 0))}) waiting in Review.`
      : "You've gone through everything in this view.";
    $("#emptyReview").classList.toggle("hidden", nothing);
    $("#emptyReview").textContent = del.length ? "Review & clean up" : "See what you kept";
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
      const src = url("/view", f.path);
      m.innerHTML = `<div class="blur"></div><div class="loading"></div><img alt="" draggable="false" decoding="async">`;
      const img = m.querySelector("img");
      img.onload = () => { m.querySelector(".loading")?.remove(); m.querySelector(".blur").style.backgroundImage = `url("${src}")`; };
      img.onerror = () => { m.innerHTML = glyph(f, "Can't preview this image here.", openBtn()); wireOpen(m, f); };
      img.src = src;
      break;
    }
    case "video": {
      const poster = url("/thumb", f.path);
      m.innerHTML = `<div class="blur" style="background-image:url('${poster}')"></div>
        <video playsinline loop preload="metadata" poster="${poster}"></video>
        <div class="badge-play hidden"></div>
        <div class="vbar"><div><i></i></div></div>`;
      const v = m.querySelector("video");
      v.src = url("/raw", f.path);
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
      loadInfo(el, f);
      break;
    }
    case "audio": {
      const bars = Array.from({ length: 16 }, (_, i) => `<span style="animation-delay:${(i * 137) % 900}ms;animation-duration:${600 + ((i * 211) % 700)}ms"></span>`).join("");
      m.innerHTML = `<div class="glyph"><div class="audio-viz">${bars}</div><div class="sub">click to play / pause</div></div>
        <audio preload="metadata" loop></audio><div class="vbar"><div><i></i></div></div>`;
      const a = m.querySelector("audio");
      a.src = url("/raw", f.path);
      a.onplay = () => m.classList.add("playing");
      a.onpause = () => m.classList.remove("playing");
      a.ontimeupdate = () => { if (a.duration) m.querySelector(".vbar i").style.width = `${(a.currentTime / a.duration) * 100}%`; };
      const bar = m.querySelector(".vbar");
      bar.onpointerdown = (e) => { e.stopPropagation(); const r = bar.getBoundingClientRect(); if (a.duration) a.currentTime = clamp((e.clientX - r.left) / r.width) * a.duration; };
      loadInfo(el, f);
      break;
    }
    case "pdf": {
      m.innerHTML = `<div class="loading"></div><img class="pdfthumb" alt="" draggable="false">`;
      const img = m.querySelector("img");
      img.style.background = "#fff";
      img.onload = () => m.querySelector(".loading")?.remove();
      img.onerror = () => { m.innerHTML = glyph(f, "PDF · press Enter to read it, or O to open.", openBtn()); wireOpen(m, f); };
      img.src = url("/thumb", f.path);
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
    p.muted = S.muted;
    p.play().catch(() => {
      // autoplay with sound blocked until the user interacts: retry muted for video
      if (p.tagName === "VIDEO") { p.muted = true; p.play().catch(() => {}); }
    });
  }
}
function deactivate(el) { const p = playerOf(el); if (p && !p.paused) p.pause(); }
function teardown(el) { const p = playerOf(el); if (p) { p.pause(); p.removeAttribute("src"); p.load(); } }

// ------------------------------------------------------------- drag -----
const TH = 110;
function attachDrag(el, f) {
  let sx = 0, sy = 0, dx = 0, dy = 0, t0 = 0, active = false, moved = false;
  const stamps = { keep: el.querySelector(".stamp.keep"), del: el.querySelector(".stamp.del"), fav: el.querySelector(".stamp.fav") };
  const tint = el.querySelector(".tint");

  el.addEventListener("pointerdown", (e) => {
    if (!el.classList.contains("top") || e.button !== 0 || e.target.closest("button, .vbar")) return;
    active = true; moved = false; sx = e.clientX; sy = e.clientY; dx = dy = 0; t0 = performance.now();
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener("pointermove", (e) => {
    if (!active) return;
    dx = e.clientX - sx; dy = e.clientY - sy;
    if (!moved && Math.hypot(dx, dy) > 6) { moved = true; el.classList.add("dragging"); }
    if (!moved) return;
    el.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 16}deg)`;
    paintStamps(stamps, tint, dx, dy);
  });
  const end = (e) => {
    if (!active) return;
    active = false;
    el.classList.remove("dragging");
    // touch screens deliver few pointermoves on a quick flick: use where the finger lifted
    if (e.type === "pointerup") { dx = e.clientX - sx; dy = e.clientY - sy; if (Math.hypot(dx, dy) > 6) moved = true; }
    if (!moved) { onTap(el); return; }
    const vertical = Math.abs(dy) > Math.abs(dx);
    const fast = Math.hypot(dx, dy) / Math.max(1, performance.now() - t0) > 0.6;  // px/ms
    const th = fast ? TH * 0.45 : TH;
    if (dx > th && !vertical) decide("keep", dx, dy);
    else if (dx < -th && !vertical) decide("delete", dx, dy);
    else if (dy < -th && vertical) decide("favorite", dx, dy);
    else if (dy > th * 1.3 && vertical) skip();
    else { el.style.transform = ""; paintStamps(stamps, tint, 0, 0); }
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
  el.style.transition = "transform .4s cubic-bezier(.3,.6,.4,1), opacity .4s";
  const tx = decision === "keep" ? w : decision === "delete" ? -w : dx;
  const ty = decision === "favorite" ? -window.innerHeight : decision === "skip" ? window.innerHeight : dy;
  el.style.transform = `translate(${tx}px, ${ty}px) rotate(${decision === "keep" ? 28 : decision === "delete" ? -28 : 0}deg)`;
  el.style.opacity = "0";
  el.classList.remove("top");
  setTimeout(() => { teardown(el); el.remove(); }, 420);
}

function decide(decision, dx = 0, dy = 0) {
  const f = S.deck.shift();
  if (!f) return;
  const el = S.els.get(f.path);
  S.els.delete(f.path);
  if (el) { deactivate(el); flyOut(el, decision, dx, dy); }
  S.history.push({ f, prev: f.decision, type: "decide" });
  f.decision = decision;
  api("/api/decide", { path: f.path, decision }).catch(() => toast("Couldn't save — is photto still running?"));
  if (decision === "delete") { const b = $("#reviewBtn"); b.classList.remove("bump"); void b.offsetWidth; b.classList.add("bump"); }
  render();
}

function skip() {
  if (S.deck.length < 2) { const el = S.els.get(S.deck[0]?.path); if (el) el.style.transform = ""; toast("This is the last one"); return; }
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
    h.f.decision = h.prev || null;
    api("/api/decide", { path: h.f.path, decision: null }).catch(() => {});
  }
  if (inGroup(h.f)) S.deck.unshift(h.f);
  else toast(`Restored ${h.f.name} (in another filter)`);
  render();
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
let reviewTab = "delete";
function openReview() {
  const top = S.deck[0]; if (top && S.els.get(top.path)) deactivate(S.els.get(top.path));
  if (!S.files.some((f) => f.decision === "delete")) reviewTab = S.files.some((f) => f.decision === "favorite") ? "favorite" : reviewTab;
  $("#review").classList.remove("hidden");
  drawReview();
}
function closeReview() {
  $("#review").classList.add("hidden");
  $("#reviewGrid").innerHTML = "";
  const top = S.deck[0]; if (top && S.els.get(top.path)) activate(S.els.get(top.path), top);
}
function drawReview() {
  const by = (d) => S.files.filter((f) => f.decision === d);
  $("#tDel").textContent = by("delete").length;
  $("#tFav").textContent = by("favorite").length;
  $("#tKeep").textContent = by("keep").length;
  document.querySelectorAll("#reviewTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === reviewTab));

  const list = by(reviewTab).sort((a, b) => b.size - a.size);
  const grid = $("#reviewGrid");
  grid.innerHTML = list.length ? "" : `<div class="none">${{ delete: "Nothing marked for deletion.", favorite: "No favorites yet — swipe up on something you love.", keep: "Nothing kept yet." }[reviewTab]}</div>`;
  const label = { delete: "Rescue", favorite: "Unfavorite", keep: "Un-keep" }[reviewTab];
  for (const f of list) {
    const t = document.createElement("div");
    t.className = "tile";
    t.style.cssText = colorVars(f.kind);
    const thumb = ["image", "image-convert", "video", "pdf"].includes(f.kind);
    t.innerHTML = `${thumb ? `<img loading="lazy" src="${url("/thumb", f.path)}" alt="">` : ""}
      <div class="tg" ${thumb ? 'style="z-index:-1"' : ""}>${esc(f.ext || f.kind)}</div>
      <div class="cap">${esc(f.name)}<small>${esc(f.sizeH)}</small></div>
      <div class="undo">↩ ${label} — put back in the deck</div>`;
    const img = t.querySelector("img");
    if (img) img.onerror = () => img.remove();
    t.onclick = () => {
      f.decision = null;
      api("/api/decide", { path: f.path, decision: null }).catch(() => {});
      if (inGroup(f)) { S.deck.unshift(f); }
      t.classList.add("gone");
      setTimeout(() => { render(); drawReview(); }, 220);
    };
    grid.appendChild(t);
  }

  const del = by("delete");
  const bytes = del.reduce((a, f) => a + f.size, 0);
  $("#freeText").textContent = del.length ? `frees ${fmtBytes(bytes)}` : "";
  const favMove = $("#favMove").checked && by("favorite").length;
  const btn = $("#commitBtn");
  btn.disabled = !del.length && !favMove;
  btn.dataset.armed = "";
  btn.textContent = del.length ? `Move ${del.length} file${del.length > 1 ? "s" : ""} to Trash` : favMove ? "Move favorites" : "Move to Trash";
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
    if (r.failed.length) console.warn("photto: failed", r.failed);
    S.history = [];
    await load(false);
    drawReview();
  } catch (e) {
    toast("Something went wrong: " + e.message);
    drawReview();
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
  S.history = []; S.shuffleKey.clear();
  await load();
  toast(`Now sorting ${shortPath(S.root)} · ${S.files.length.toLocaleString()} files`);
}
$("#folderBtn").onclick = openPicker;
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
async function load(reshuffle = true) {
  $("#scanning").classList.remove("hidden");
  let d;
  try { d = await api("/api/files"); } finally { $("#scanning").classList.add("hidden"); }
  setPlatform(d.platform);
  if (d.needsPermission) {
    S.root = null; S.files = []; S.deck = [];
    $("#welcomeTitle").textContent = "Allow access to your files";
    $("#welcomeText").textContent = "photto needs \u201cAll files access\u201d to show and tidy your folders. Nothing leaves your phone.";
    $("#welcomePick").textContent = "Allow access";
    $("#welcomePick").onclick = () => api("/api/permission", {});
    buildDeck();
    return;
  }
  $("#welcomeTitle").textContent = "Pick a folder to tidy";
  $("#welcomeText").textContent = "Swipe through every photo, video, song and document in it \u2014 keep, favorite or trash.";
  $("#welcomePick").textContent = "Choose folder\u2026";
  $("#welcomePick").onclick = openPicker;
  if (d.needsPick) {
    S.root = null; S.files = []; S.deck = [];
    $("#folder").textContent = "Pick a folder";
    $("#scopeTag").textContent = "";
    renderWelcome(d.recent || []);
    buildDeck();
    openPicker();
    return;
  }
  if (d.truncated) toast(`Big folder: showing the first ${d.files.length.toLocaleString()} files`);
  S.files = d.files;
  if (reshuffle || !S.shuffleKey.size) S.files.forEach((f) => S.shuffleKey.set(f.path, Math.random()));
  else S.files.forEach((f) => { if (!S.shuffleKey.has(f.path)) S.shuffleKey.set(f.path, Math.random()); });
  S.root = d.root; S.recursive = d.recursive;
  setPlatform(d.platform);
  $("#folder").textContent = shortPath(d.root);
  $("#folderBtn").title = `${d.root} — click to change folder (G)`;
  $("#scopeTag").textContent = d.recursive ? "+ subfolders" : "top only";
  document.title = `photto · ${d.name}`;
  buildDeck();
}

function setPlatform(pf) {
  if (!pf || S.platform === pf) return;
  S.platform = pf;
  document.body.dataset.platform = pf;
}

// Android back button / Escape-like behaviour; returns true if something was closed.
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
  load();
};
$("#reviewClose").onclick = closeReview;
$("#review").onclick = (e) => { if (e.target.id === "review") closeReview(); };
$("#reviewTabs").onclick = (e) => { const b = e.target.closest("button"); if (b) { reviewTab = b.dataset.tab; drawReview(); } };
$("#favMove").onchange = drawReview;
$("#commitBtn").onclick = commit;
$("#viewerClose").onclick = closeViewer;
$("#viewerOpen").onclick = () => viewerFile && openExternal(viewerFile);
$("#viewerFolder").onclick = () => viewerFile && openExternal(viewerFile, true);
const sortSel = $("#sort");
sortSel.value = S.sort;
sortSel.onchange = () => {
  S.sort = sortSel.value; localStorage.setItem("photto.sort", S.sort);
  if (S.sort === "shuffle") S.files.forEach((f) => S.shuffleKey.set(f.path, Math.random()));
  buildDeck(); sortSel.blur();
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
    Missing access token. Open the full URL printed in the terminal by <code>photto</code>.</div>`;
} else {
  load().catch((e) => toast("Couldn't load files: " + e.message));
  // heartbeat: lets photto quit by itself once this window is closed
  let lost = 0;
  setInterval(() => {
    fetch("/api/ping", { headers: { "X-Token": TOKEN } })
      .then(() => { if (lost) { lost = 0; $("#gone").classList.add("hidden"); } })
      .catch(() => { if (++lost >= 2) $("#gone").classList.remove("hidden"); });
  }, 5000);
}
$("#welcomePick").onclick = openPicker;
