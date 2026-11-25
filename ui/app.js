// Autosynced Lyrics app window. Talks to the running app through its local API (app_server.py).
"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function api(path, body) {
  const opt = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch("/api/" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.add("on");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("on"), 2200);
}

function confirmBox(title, text, yes = "Delete") {
  return new Promise(resolve => {
    $("#modalTitle").textContent = title;
    $("#modalText").textContent = text;
    $("#modalYes").textContent = yes;
    $("#modal").hidden = false;
    const done = v => { $("#modal").hidden = true; resolve(v); };
    $("#modalYes").onclick = () => done(true);
    $("#modalNo").onclick = () => done(false);
  });
}

const fmtTime = s => {
  s = Math.max(0, Math.floor(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const money = x => !x ? "$0" : x < 0.0001 ? "<$0.0001" : x < 1 ? `$${x.toFixed(4)}` : `$${x.toFixed(2)}`;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------------------------------------------------------- pages
function showPage(name) {
  $$(".nav").forEach(b => b.classList.toggle("on", b.dataset.page === name));
  $$(".page").forEach(p => p.classList.toggle("on", p.id === "page-" + name));
  if (name === "library") loadLibrary();
  if (name === "settings") loadSettings();
  if (name === "usage") loadUsage();
  if (name === "now") requestAnimationFrame(() => scrollToNow(true));
  else if (document.body.classList.contains("full")) setFull(false);
  try { localStorage.setItem("page", name); } catch (e) {}
}

// ---------------------------------------------------------------- full screen
// Now Playing with just the cover and the lyrics, filling the screen. In the app window
// the window itself goes full screen (pywebview); in a browser, the page does.
async function setFull(on) {
  document.body.classList.toggle("full", on);
  $("#btnFull").title = on ? "Exit full screen (Esc)" : "Full screen (F11)";
  try {
    if (window.pywebview && window.pywebview.api.set_fullscreen) await window.pywebview.api.set_fullscreen(on);
    else if (on && !document.fullscreenElement) await document.documentElement.requestFullscreen();
    else if (!on && document.fullscreenElement) await document.exitFullscreen();
  } catch (e) {}
  requestAnimationFrame(() => scrollToNow(true));
}
$("#btnFull").onclick = () => setFull(!document.body.classList.contains("full"));
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement && !window.pywebview && document.body.classList.contains("full")) setFull(false);
});
document.addEventListener("keydown", e => {
  if (e.target.closest && e.target.closest("input, textarea, select")) return;
  const full = document.body.classList.contains("full");
  if (e.key === "F11" && $("#page-now").classList.contains("on")) { e.preventDefault(); setFull(!full); }
  else if (e.key === "Escape" && full) { e.preventDefault(); setFull(false); }
});
$$(".nav").forEach(b => b.addEventListener("click", () =>
  b.dataset.page === "editor" ? openEditor() : showPage(b.dataset.page)));

// ---------------------------------------------------------------- style
let style = "night";
function applyStyle(s) {
  if (s !== "night" && s !== "day") s = "night";
  style = s;
  document.body.dataset.theme = s;
  $("#styleName").textContent = s === "night" ? "Night" : "Day";
  $$(".style-card").forEach(c => c.classList.toggle("on", c.dataset.style === s));
  try { window.pywebview && window.pywebview.api.set_style(s); } catch (e) {}   // title bar colour
}
$("#styleSwitch").addEventListener("click", () => setStyle(style === "night" ? "day" : "night"));
async function setStyle(s) {
  applyStyle(s);
  try { await api("settings", { ui_style: s }); } catch (e) { toast("Couldn't save the style"); }
}

// ---------------------------------------------------------------- album cover
// One template for the Now Playing cover and the Settings preview.
const COVER_HTML = `
  <div class="mover"><div class="disc">
    <div class="grooves"></div>
    <div class="label"><img class="cover-img" alt=""><div class="cover-ph"><svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div></div>
    <div class="sheen"></div><div class="hole"></div><div class="caption"></div>
  </div></div>
  <svg class="arm" viewBox="0 0 100 220" aria-hidden="true">
    <circle cx="62" cy="22" r="16" class="arm-base"/><circle cx="62" cy="22" r="6" class="arm-pivot"/>
    <path d="M62 22 L66 150 L46 196" class="arm-rod"/>
    <rect x="34" y="186" width="22" height="30" rx="4" transform="rotate(28 45 201)" class="arm-head"/>
  </svg>`;

const COVER_PRESETS = {
  "Vinyl":            { shape: "vinyl",    motion: "spin",  speed: 7,  arm: true,  glow: false, reflect: false },
  "Spinning picture": { shape: "circle",   motion: "spin",  speed: 12, arm: false, glow: false, reflect: false },
  "CD":               { shape: "cd",       motion: "spin",  speed: 4,  arm: false, glow: true,  reflect: false },
  "Polaroid":         { shape: "polaroid", motion: "sway",  speed: 9,  arm: false, glow: false, reflect: false },
  "Floating":         { shape: "rounded",  motion: "float", speed: 6,  arm: false, glow: true,  reflect: false },
  "Breathing":        { shape: "circle",   motion: "pulse", speed: 7,  arm: false, glow: true,  reflect: false },
  "Mirror":           { shape: "square",   motion: "tilt",  speed: 7,  arm: false, glow: false, reflect: true },
  "Classic":          { shape: "square",   motion: "still", speed: 7,  arm: false, glow: false, reflect: false },
};
let coverCfg = { source: "album", ...COVER_PRESETS["Vinyl"] }, coverUrl = null, glowColor = "rgba(255,255,255,.25)";
let dominantHue = null;

// A soft colour mix for when the album art isn't shown: built from the album's main colour,
// or (no art) from the song's name, so every song gets its own, always the same, colours.
function mixCss() {
  let h = dominantHue != null ? dominantHue * 360 : 0;
  if (dominantHue == null) {
    const str = `${now.title || ""}\u0000${now.artist || ""}`;
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
  }
  const hue = d => Math.round((h + d + 360) % 360);
  return [`radial-gradient(circle at 18% 22%, hsl(${hue(0)} 90% 64%) 0, transparent 52%)`,
          `radial-gradient(circle at 84% 16%, hsl(${hue(48)} 88% 62%) 0, transparent 50%)`,
          `radial-gradient(circle at 72% 86%, hsl(${hue(-52)} 85% 58%) 0, transparent 56%)`,
          `radial-gradient(circle at 16% 92%, hsl(${hue(150)} 70% 55%) 0, transparent 44%)`,
          `linear-gradient(135deg, hsl(${hue(0)} 60% 36%), hsl(${hue(-52)} 62% 26%))`].join(", ");
}

// The cover's other appearances follow the Picture choice too: player-bar thumbnail and
// the blurred backgrounds (they'd otherwise show the art you chose to hide).
function updateSurfaces() {
  const src = coverCfg.source || "album", mix = mixCss();
  const small = $(".cover.small");
  small.style.setProperty("--mix", mix);
  small.classList.toggle("mix", src === "gradient");
  small.classList.toggle("hidden", src === "none");
  $(".np").classList.toggle("no-cover", src === "none");
  const art = src === "album" && coverUrl ? `url(${coverUrl})` : (now.open ? mix : "");
  $("#backdrop .art").style.backgroundImage = art;
  $(".np-art").style.backgroundImage = art;
  $("#backdrop").classList.toggle("on", !!art);
}

function buildCover(el) {
  el.innerHTML = COVER_HTML;
  const img = el.querySelector(".cover-img");
  if (coverUrl) { img.onload = () => img.classList.add("loaded"); img.src = coverUrl; }
  return el;
}

function applyCover(el, cfg, playing) {
  el.dataset.source = cfg.source || "album";
  el.style.setProperty("--mix", mixCss());
  el.dataset.shape = cfg.shape;
  el.dataset.motion = cfg.motion;
  el.style.setProperty("--speed", `${Math.max(1, +cfg.speed || 7)}s`);
  el.style.setProperty("--glow", glowColor);
  el.classList.toggle("playing", !!playing);
  el.classList.toggle("arm-on", !!cfg.arm);
  el.classList.toggle("glow-on", !!cfg.glow);
  el.classList.toggle("reflect-on", !!cfg.reflect);
  const cap = el.querySelector(".caption");
  if (cap) cap.textContent = now.open ? now.title : "";
}

// "Tilt" follows the mouse over the cover area.
document.addEventListener("mousemove", e => {
  $$('.record[data-motion="tilt"]').forEach(el => {
    const r = el.getBoundingClientRect();
    const x = (e.clientX - (r.left + r.width / 2)) / Math.max(200, window.innerWidth / 2);
    const y = (e.clientY - (r.top + r.height / 2)) / Math.max(200, window.innerHeight / 2);
    el.style.setProperty("--ry", `${Math.max(-1, Math.min(1, x)) * 18}deg`);
    el.style.setProperty("--rx", `${Math.max(-1, Math.min(1, -y)) * 14}deg`);
  });
});

// ---------------------------------------------------------------- now playing
let now = { open: false }, nowAt = performance.now();
let lyr = { rev: null, lines: [], synced: false };
let coverId = null, activeIdx = -2, userScrollUntil = 0;

function position() {            // song position, moving smoothly between polls
  if (!now.open) return 0;
  const p = now.position + (now.playing ? (performance.now() - nowAt) / 1000 : 0);
  return Math.min(p, now.duration || p);
}

async function poll() {
  try {
    const n = await api("now");
    now = n; nowAt = performance.now();
    if (n.style && n.style !== style) applyStyle(n.style);
    if (n.goto && n.goto.page === "editor") openEditor(n.goto.title, n.goto.artist);
    else if (n.goto && n.goto.page) showPage(n.goto.page);
    renderNow();
    renderUpdate(n.update);
    if (n.open && n.lyrics_rev !== lyr.rev) await loadLyrics();
    if (!n.open && lyr.rev !== "") { lyr = { rev: "", lines: [], synced: false }; renderLyrics(); }
  } catch (e) {
    now = { open: false, waiting: "Can't reach Autosynced Lyrics. Is it still running?" };
    renderNow();
  }
  setTimeout(poll, calm() ? 1500 : 400);
}

// ---- "a new version is available" -----------------------------------------
let updLaterFor = null;          // the version you said "later" to (until the app restarts)
function renderUpdate(u) {
  const banner = $("#updBanner"), go = $("#updGo");
  const st = (u && u.state) || "";
  const busy = st.startsWith("downloading") || st === "installing";
  if (!u || (updLaterFor === u.version && !busy)) { banner.hidden = true; return; }
  banner.hidden = false;
  $("#updText").textContent =
      st.startsWith("downloading") ? `Downloading version ${u.version}… ${st.split(" ")[1] || ""}`
    : st === "installing" ? "Installing… the app will restart in a moment"
    : st.startsWith("error: ") ? st.slice(7)
    : `Version ${u.version} is available` + (u.small_ok ? "" : " (full download)");
  go.hidden = busy;
  go.textContent = st.startsWith("error: ") ? "Try again" : u.small_ok ? "Update now" : "Download";
  go.title = u.notes || "";
  $("#updLater").hidden = busy;
  banner.dataset.version = u.version;
}
$("#updGo").onclick = async () => {
  try {
    const r = await api("update/install", {});
    if (r.opened) toast("Opened the download page in your browser");
    else if (r.error) toast(r.error);
  } catch (e) { toast("The update didn't start"); }
};
$("#updLater").onclick = () => { updLaterFor = $("#updBanner").dataset.version; $("#updBanner").hidden = true; };

function renderNow() {
  const open = now.open;
  $("#npTitle").textContent = open ? now.title : "Nothing playing";
  $("#npArtist").textContent = open ? now.artist : (now.waiting || "");
  $("#npPlayer").textContent = open ? `Playing on ${now.player}` : "Not playing";
  $("#plTitle").textContent = open ? now.title : "Nothing playing";
  $("#plArtist").textContent = open ? now.artist : "";
  $("#tEnd").textContent = fmtTime(open ? now.duration : 0);
  $("#btnPlay").classList.toggle("playing", !!(open && now.playing));
  const v = open ? now.song_fix || 0 : 0;
  $("#nudgeVal").textContent = v ? `Timing ${v > 0 ? "+" : ""}${v.toFixed(2)} s` : "Timing 0.00 s";
  $("#btnBar span").textContent = now.bar_hidden ? "Show lyrics bar" : "Hide lyrics bar";
  if (now.cover && typeof now.cover === "object") coverCfg = now.cover;
  applyCover($("#record"), coverCfg, open && now.playing);
  const pv = $(".cover-preview .record");
  if (pv) applyCover(pv, coverCfg, true);
  updateSurfaces();
  const id = open ? now.cover_id : "";
  if (id !== coverId) { coverId = id; loadCover(id); }
}

function loadCover(id) {
  const imgs = [...$$(".record .cover-img"), $("#coverSmall")];
  if (!id) {
    coverUrl = null;
    imgs.forEach(i => { i.classList.remove("loaded"); i.removeAttribute("src"); });
    $("#backdrop").classList.remove("on");
    $(".np-art").style.backgroundImage = "";
    setTint(null);
    return;
  }
  // The token goes in a header, so fetch the picture and show it from a blob URL.
  fetch(`/api/cover?id=${id}`, { headers: { "X-Token": TOKEN } })
    .then(r => r.ok ? r.blob() : Promise.reject())
    .then(b => {
      const url = URL.createObjectURL(b);
      coverUrl = url;
      imgs.forEach(i => { i.onload = () => i.classList.add("loaded"); i.src = url; });
      $("#backdrop .art").style.backgroundImage = `url(${url})`;
      $(".np-art").style.backgroundImage = `url(${url})`;
      $("#backdrop").classList.add("on");
      const probe = new Image();
      probe.onload = () => { setTint(dominant(probe)); updateSurfaces(); };
      probe.src = url;
      updateSurfaces();
    })
    .catch(() => imgs.forEach(i => i.classList.remove("loaded")));
}

function dominant(img) {         // the cover's most striking colour, for the night lyrics panel
  const c = document.createElement("canvas"), n = 32;
  c.width = c.height = n;
  const g = c.getContext("2d");
  g.drawImage(img, 0, 0, n, n);
  const d = g.getImageData(0, 0, n, n).data;
  let best = null, bins = {};
  for (let i = 0; i < d.length; i += 4) {
    const [h, s, l] = rgbToHsl(d[i], d[i + 1], d[i + 2]);
    if (s < .25 || l < .15 || l > .85) continue;
    const k = Math.round(h * 18), w = s * (1 - Math.abs(l - .5));
    const b = bins[k] || (bins[k] = { w: 0, h: 0, s: 0 });
    b.w += w; b.h += h * w; b.s += s * w;
    if (!best || b.w > best.w) best = b;
  }
  return best ? { h: best.h / best.w, s: best.s / best.w } : null;
}
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
  if (mx === mn) return [0, 0, l];
  const d = mx - mn, s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
  const h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h / 6, s, l];
}
function setTint(c) {
  dominantHue = c ? c.h : null;
  glowColor = c ? `hsla(${Math.round(c.h * 360)} ${Math.round(Math.min(.9, c.s + .1) * 100)}% 58% / .55)` : "rgba(255,255,255,.25)";
  $$(".record").forEach(el => el.style.setProperty("--glow", glowColor));
  const css = c ? `hsl(${Math.round(c.h * 360)} ${Math.round(Math.min(.6, c.s) * 100)}% 26%)` : "#3a3a3a";
  document.body.style.setProperty("--np-tint", css);
}

async function loadLyrics() {
  try { lyr = await api("lyrics"); } catch (e) { return; }
  renderLyrics();
}

function renderLyrics() {
  const box = $("#lyrics"), empty = $("#lyricsEmpty");
  box.innerHTML = "";
  activeIdx = -2;
  box.classList.toggle("unsynced", !lyr.synced);
  if (!lyr.lines.length) {
    empty.hidden = false;
    empty.innerHTML = now.open
      ? `${esc(lyr.status || now.status || "Loading lyrics…")}`
      : "Play a song and its lyrics will appear here.";
    return;
  }
  empty.hidden = true;
  const frag = document.createDocumentFragment();
  lyr.lines.forEach((l, i) => {
    const d = document.createElement("div");
    d.className = "line";
    d.dataset.i = i;
    d.innerHTML = `<div>${esc(l.text || "♪")}</div>` +
      (l.tr ? `<div class="tr"><span>${esc(l.tr)}</span>${l.ai ? '<span class="ai-tag" title="Not from a lyrics site: translated by AI or machine translation">AI</span>' : ""}</div>` : "");
    if (lyr.synced) d.addEventListener("click", () => seekTo(l.t, true));
    frag.appendChild(d);
  });
  box.appendChild(frag);
  if (!lyr.synced) {
    const note = document.createElement("div");
    note.className = "lyrics-empty";
    note.innerHTML = `These lyrics have no timing yet.<br><button class="btn primary">Sync them</button>`;
    note.querySelector("button").onclick = () => openEditor(now.title, now.artist);
    box.prepend(note);
  }
  tick(true);
}

function lineAt(t) {
  let lo = 0, hi = lyr.lines.length - 1, ans = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (lyr.lines[m].t <= t) { ans = m; lo = m + 1; } else hi = m - 1;
  }
  return ans;
}

let tickPending = false, shownPct = -1, shownTime = "";
function tick(force) {
  const p = position();
  const dur = now.open ? now.duration : 0;
  const frac = dur ? Math.min(1, p / dur) : 0;
  const pct = Math.round(frac * 1000) / 10;          // only redraw when it visibly moves
  if (pct !== shownPct) {
    shownPct = pct;
    $("#progressFill").style.width = pct + "%";
    $("#progressKnob").style.left = pct + "%";
  }
  const tt = fmtTime(p);
  if (tt !== shownTime) { shownTime = tt; $("#tNow").textContent = tt; }
  if (lyr.synced && lyr.lines.length) {
    const i = lineAt(p + (now.fix || 0));
    if (i !== activeIdx || force) {
      activeIdx = i;
      $$(".line", $("#lyrics")).forEach(el => {
        const k = +el.dataset.i;
        el.classList.toggle("now", k === i);
        el.classList.toggle("past", k < i);
      });
      scrollToNow(false);
    }
  }
  if (!force && !tickPending) {
    tickPending = true;
    const next = () => { tickPending = false; tick(false); };
    // smooth (every frame) while you're looking; 4 times a second otherwise
    if (calm()) setTimeout(next, 250); else requestAnimationFrame(next);
  }
}

function scrollToNow(instant) {
  const panel = $("#lyricsPanel");
  const el = $(`.line[data-i="${Math.max(0, activeIdx)}"]`, panel);
  if (!el || performance.now() < userScrollUntil) return;
  const top = el.offsetTop - panel.clientHeight * 0.36;
  panel.scrollTo({ top: Math.max(0, top), behavior: instant ? "auto" : "smooth" });
}
$("#lyricsPanel").addEventListener("wheel", () => {
  userScrollUntil = performance.now() + 4000;
  $("#backToNow").classList.add("on");
  clearTimeout(scrollToNow.t);
  scrollToNow.t = setTimeout(() => { $("#backToNow").classList.remove("on"); scrollToNow(false); }, 4000);
}, { passive: true });
$("#backToNow").addEventListener("click", () => {
  userScrollUntil = 0;
  $("#backToNow").classList.remove("on");
  scrollToNow(false);
});

async function seekTo(t, lyricTime) {
  if (!now.open) return;
  if (!now.can_seek) { toast("This music app doesn't allow jumping to a position"); return; }
  try {
    await api("control", { name: "seek", to: t, raw: !lyricTime });
    now.position = lyricTime ? t - (now.fix || 0) : t;
    nowAt = performance.now();
    userScrollUntil = 0;
  } catch (e) { toast("Couldn't jump there"); }
}
$("#progress").addEventListener("click", e => {
  const r = e.currentTarget.getBoundingClientRect();
  if (now.open && now.duration) seekTo((e.clientX - r.left) / r.width * now.duration, false);
});

const control = name => api("control", { name }).catch(() => toast("The music app didn't respond"));
$("#btnPrev").onclick = () => control("prev");
$("#btnNext").onclick = () => control("next");
$("#btnPlay").onclick = () => { now.playing = !now.playing; $("#btnPlay").classList.toggle("playing", now.playing); control("play"); };
$("#btnEarlier").onclick = () => control("earlier").then(() => toast("This song's lyrics: 0.25 s earlier"));
$("#btnLater").onclick = () => control("later").then(() => toast("This song's lyrics: 0.25 s later"));
$("#btnEdit").onclick = () => now.open ? openEditor(now.title, now.artist) : openEditor();
$("#btnBar").onclick = () => control("toggle_bar");

// ---------------------------------------------------------------- my lyrics
async function loadLibrary() {
  let data;
  try { data = await api("mylyrics"); } catch (e) { return; }
  const tb = $("#libTable tbody");
  tb.innerHTML = "";
  $("#libEmpty").hidden = data.songs.length > 0;
  $("#libSub").textContent = data.songs.length
    ? `${data.songs.length} song${data.songs.length === 1 ? "" : "s"} you synced yourself. They always take priority over lyrics found online.`
    : "Lyrics you synced yourself. They always take priority over lyrics found online.";
  for (const s of data.songs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="title">${esc(s.title)}</td><td class="muted">${esc(s.artist)}</td>
      <td class="num">${s.lines}</td>
      <td>${s.synced ? '<span class="badge ok">Synced</span>' : '<span class="badge plain">Not timed</span>'}</td>
      <td class="muted">${esc(s.modified)}</td>
      <td><div class="row-actions">
        <button class="icon-btn small" title="Edit" data-a="edit"><svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></button>
        <button class="icon-btn small" title="Delete" data-a="del"><svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg></button>
      </div></td>`;
    tr.querySelector('[data-a="edit"]').onclick = () => openEditor(s.title, s.artist);
    tr.querySelector('[data-a="del"]').onclick = async () => {
      if (await confirmBox(`Delete "${s.title}"?`, `Your synced lyrics for ${s.title} — ${s.artist} will be deleted for good. The song will use lyrics found online again.`)) {
        try { await api("mylyrics/delete", { file: s.file }); toast("Deleted"); loadLibrary(); }
        catch (e) { toast("Couldn't delete it"); }
      }
    };
    tb.appendChild(tr);
  }
}
$("#libOpenEditor").onclick = () => { $("#edTitle").value = ""; openEditor(); };
$("#libToPhone").onclick = async () => {
  try {
    const r = await api("mylyrics/export", {});
    if (!r.count) { toast("No saved lyrics to send yet"); return; }
    toast(`${r.count} song${r.count === 1 ? "" : "s"} saved to ${r.name} in Downloads. Send it to your phone, then tap Import in the Android app's settings.`);
  } catch (e) { toast("Couldn't make the zip"); }
};

// ---------------------------------------------------------------- settings
const SECTIONS = [
  { title: "Appearance", items: [
    { type: "styles" },
    { type: "cover" },
    { type: "toggle", key: "bar_follows_style", label: "Lyrics bar uses the style's colours", hint: "Turn off to pick your own colours below" },
    { type: "toggle", key: "low_cpu", label: "Low CPU mode", hint: "Plainer app window: no blurred backgrounds, reflections or spinning cover, fewer redraws. The window already goes quiet by itself when it isn't in front." },
  ]},
  { title: "Lyrics bar · look", items: [
    { type: "range", key: "opacity", label: "Overall opacity", min: .2, max: 1, step: .05, pct: true },
    { type: "range", key: "bg_opacity", label: "Background opacity", hint: "0 = the words float on the desktop", min: 0, max: 1, step: .05, pct: true },
    { type: "color", key: "bg_color", label: "Background colour", own: true },
    { type: "toggle", key: "soft_background", label: "Soft background with a shadow", hint: "A soft glow behind the words when there's no background" },
    { type: "toggle", key: "album_colors", label: "Colours from the album cover", hint: "Current line and background" },
    { type: "select", key: "font_family", label: "Font", choices: "font_family" },
    { type: "toggle", key: "outline_enabled", label: "Outline the lyrics" },
    { type: "color", key: "outline_color", label: "Outline colour", own: true },
    { type: "number", key: "outline_width", label: "Outline thickness", min: 1, max: 8, step: 1 },
    { type: "range", key: "width_pct", label: "Width", hint: "% of the screen", min: 20, max: 100, step: 5, unit: "%" },
  ]},
  { title: "Lyrics bar · text", items: [
    { type: "toggle", key: "show_progress", label: "Song progress line along the bottom" },
    { type: "toggle", key: "show_header", label: "Show title and artist on top" },
    { type: "number", key: "header_size", label: "Title size", min: 6, max: 48, step: 1 },
    { type: "color", key: "header_color", label: "Title colour", own: true },
    { type: "number", key: "current_size", label: "Current line size", min: 8, max: 96, step: 1 },
    { type: "toggle", key: "bold_current", label: "Current line in bold" },
    { type: "color", key: "current_color", label: "Current line colour", own: true },
    { type: "toggle", key: "show_prev", label: "Show the previous line" },
    { type: "toggle", key: "show_next", label: "Show the next line" },
    { type: "number", key: "other_size", label: "Previous / next size", min: 6, max: 72, step: 1 },
    { type: "color", key: "other_color", label: "Previous / next colour", own: true },
  ]},
  { title: "Chinese translation", items: [
    { type: "toggle", key: "show_translation", label: "Show the Chinese translation under each line" },
    { type: "range", key: "translation_size_pct", label: "Translation size", hint: "% of the line", min: 30, max: 100, step: 5, unit: "%" },
    { type: "toggle", key: "ai_translate", label: "Fill lines with no translation", hint: "Lines no lyrics site has translated are translated for you, marked with an AI tag." },
    { type: "select", key: "ai_provider", label: "Translated by", choices: "ai_provider", hint: "Claude, DeepSeek or Qwen: 信达雅 style, with your own API key (pay-as-you-go). Machine translation: free and needs no key, but word-for-word." },
    { type: "apikey" },
  ]},
  { title: "Animation", items: [
    { type: "select", key: "animation", label: "Line change effect", choices: "animation" },
    { type: "range", key: "animation_ms", label: "Effect duration", min: 100, max: 1200, step: 50, unit: " ms" },
    { type: "toggle", key: "bouncing_note", label: "Bouncing note", hint: "A note hops along the words as they're sung" },
    { type: "number", key: "note_offset", label: "Note timing", hint: "Seconds; + = earlier (try −0.15 with Bluetooth headphones)", min: -.5, max: .5, step: .02 },
    { type: "toggle", key: "fix_timing_by_listening", label: "Fix badly timed lyrics by listening", hint: "Lyrics found online can be timed for another version of the song. While it plays, lines are moved to when they're actually sung (remembered after the first play)." },
    { type: "toggle", key: "learn_word_timing", label: "Learn word timing by listening", hint: "When no lyrics site has it. Uses some CPU while a new song plays." },
    { type: "number", key: "offset", label: "Lyrics offset for all songs", hint: "Seconds; + = earlier", min: -10, max: 10, step: .1 },
  ]},
  { title: "Behaviour", items: [
    { type: "select", key: "player", label: "Music app", choices: "player" },
    { type: "toggle", key: "hide_when_closed", label: "Hide the lyrics bar when no music app is open" },
    { type: "toggle", key: "hover_controls", label: "Controls when the mouse is over the lyrics bar" },
    { type: "toggle", key: "lock_position", label: "Lock the lyrics bar in place", hint: "Clicks pass through it; press Ctrl over it for the controls" },
    { type: "startup" },
    { type: "toggle", key: "cache_lyrics", label: "Remember lyrics found online", hint: "Replayed songs load instantly; at most 5 MB" },
    { type: "cache" },
  ]},
  { title: "About", items: [
    { type: "about" },
    { type: "toggle", key: "check_updates", label: "Check for updates automatically", hint: "Looks on GitHub when the app starts and every 6 hours" },
    { type: "updates" },
  ]},
];

let S = null;
async function loadSettings() {
  try { S = await api("settings"); } catch (e) { toast("Couldn't load settings"); return; }
  applyStyle(S.settings.ui_style);
  setLowCpu(S.settings.low_cpu);
  renderSettings();
}

// ---- keeping CPU use down ----------------------------------------------------
// "quiet": the window isn't in front (or is minimised), so nobody is watching closely:
// pause animations and redraw a few times a second instead of 60. "low-cpu": the same
// cuts all the time, plus no expensive blur / reflection effects (Settings).
let lowCpu = false, quiet = !document.hasFocus();
function setLowCpu(on) { lowCpu = !!on; document.body.classList.toggle("low-cpu", lowCpu); }
function setQuiet(on) {
  if (on === quiet) return;
  quiet = on;
  document.body.classList.toggle("quiet", quiet);
  if (!quiet) tick(false);              // back to smooth updates straight away
}
const calm = () => lowCpu || quiet || document.hidden;
window.addEventListener("blur", () => setQuiet(true));
window.addEventListener("focus", () => setQuiet(false));
document.addEventListener("visibilitychange", () => setQuiet(document.hidden || !document.hasFocus()));
document.body.classList.toggle("quiet", quiet);

function setPct(input) {
  const p = (input.value - input.min) / (input.max - input.min) * 100;
  input.style.setProperty("--p", p + "%");
}

let saveTimer = null, pending = {};
function save(key, value) {
  S.settings[key] = value;
  if (key === "low_cpu") setLowCpu(value);
  pending[key] = value;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const body = pending; pending = {};
    try { await api("settings", body); } catch (e) { toast("Couldn't save that setting"); }
  }, 120);
  if (key === "bar_follows_style") renderSettings();
}

function renderSettings() {
  const form = $("#settingsForm");
  const s = S.settings;
  form.innerHTML = "";
  for (const sec of SECTIONS) {
    const box = document.createElement("div");
    box.className = "set-section";
    box.innerHTML = `<h2>${sec.title}</h2>`;
    if (sec.title === "Lyrics bar · look" && s.bar_follows_style)
      box.insertAdjacentHTML("beforeend", `<p class="set-note">Colours come from the ${s.ui_style === "night" ? "Night" : "Day"} style. Turn off "Lyrics bar uses the style's colours" to choose your own.</p>`);
    for (const it of sec.items) box.appendChild(makeRow(it, s));
    form.appendChild(box);
  }
}

function row(label, hint, ctl, disabled) {
  const r = document.createElement("div");
  r.className = "row" + (disabled ? " disabled" : "");
  r.innerHTML = `<div class="row-label">${esc(label)}${hint ? `<small>${esc(hint)}</small>` : ""}</div>`;
  const c = document.createElement("div");
  c.className = "ctl";
  if (ctl) c.appendChild(ctl);
  r.appendChild(c);
  return r;
}

function makeRow(it, s) {
  const own = it.own && s.bar_follows_style;
  if (it.type === "styles") {
    const wrap = document.createElement("div");
    wrap.className = "style-cards";
    wrap.innerHTML = [["night", "Night", "Spotify-style: dark, with green accents"],
                      ["day", "Day", "Apple Music-style: frosted, rose accents, immersive lyrics"]]
      .map(([k, name, d]) => `<button class="style-card${s.ui_style === k ? " on" : ""}" data-style="${k}">
        <div class="pv pv-${k}"><div class="s"></div><div class="m"><div class="l1"></div><div class="l2"></div><div class="l2" style="width:40%"></div><div class="dot"></div></div></div>
        <div class="cap"><b>${name}</b><span>${d}</span></div></button>`).join("");
    $$(".style-card", wrap).forEach(b => b.onclick = () => { setStyle(b.dataset.style); S.settings.ui_style = b.dataset.style; renderSettings(); });
    return wrap;
  }
  if (it.type === "toggle") {
    const sw = document.createElement("button");
    sw.className = "switch" + (s[it.key] ? " on" : "");
    sw.setAttribute("role", "switch");
    sw.onclick = () => { const v = !S.settings[it.key]; sw.classList.toggle("on", v); save(it.key, v); };
    return row(it.label, it.hint, sw);
  }
  if (it.type === "range") {
    const frag = document.createElement("div");
    frag.className = "ctl";
    const inp = document.createElement("input");
    Object.assign(inp, { type: "range", min: it.min, max: it.max, step: it.step, value: s[it.key] });
    const val = document.createElement("span");
    val.className = "val";
    const show = () => { val.textContent = it.pct ? Math.round(inp.value * 100) + "%" : inp.value + (it.unit || ""); setPct(inp); };
    inp.oninput = () => { show(); save(it.key, it.step >= 1 ? parseInt(inp.value) : parseFloat(inp.value)); };
    show();
    frag.append(inp, val);
    return row(it.label, it.hint, frag);
  }
  if (it.type === "number") {
    const inp = document.createElement("input");
    Object.assign(inp, { type: "number", min: it.min, max: it.max, step: it.step, value: s[it.key] });
    inp.onchange = () => {
      let v = Math.min(it.max, Math.max(it.min, parseFloat(inp.value) || 0));
      v = it.step >= 1 ? Math.round(v) : Math.round(v * 100) / 100;
      inp.value = v; save(it.key, v);
    };
    return row(it.label, it.hint, inp);
  }
  if (it.type === "color") {
    const inp = document.createElement("input");
    Object.assign(inp, { type: "color", value: s[it.key] });
    inp.oninput = () => save(it.key, inp.value);
    return row(it.label, own ? "Set by the style" : it.hint, inp, own);
  }
  if (it.type === "select") {
    const sel = document.createElement("select");
    const ch = S.choices[it.choices];
    for (const c of ch) {
      const [label, value] = Array.isArray(c) ? c : [c, c];
      const o = document.createElement("option");
      o.value = value; o.textContent = label;
      if (value === s[it.key]) o.selected = true;
      sel.appendChild(o);
    }
    sel.onchange = () => { save(it.key, sel.value); if (it.key === "ai_provider") renderSettings(); };
    return row(it.label, it.hint, sel);
  }
  if (it.type === "apikey") {
    const prov = s.ai_provider || "claude";
    const names = Object.fromEntries((S.choices.ai_provider || []).map(([n, v]) => [v, n]));
    const name = names[prov] || "Claude";
    if (prov === "machine")
      return row("No key needed", "Free (MyMemory, about 5,000 characters a day; translated lines are kept, so replays are free)." +
                 (S.ai_status ? " " + S.ai_status : ""), null);
    const has = !!(S.ai_keys || {})[prov];
    const wrap = document.createElement("div");
    wrap.className = "ctl";
    const inp = document.createElement("input");
    Object.assign(inp, { type: "password", placeholder: has ? "Saved — paste to replace" : "Paste your key", autocomplete: "off" });
    inp.style.width = "220px";
    const saveBtn = document.createElement("button");
    saveBtn.className = "btn small primary"; saveBtn.textContent = "Save";
    saveBtn.onclick = async () => {
      if (!inp.value.trim()) return;
      try { const r = await api("ai-key", { key: inp.value.trim(), provider: prov }); S.ai_keys = { ...S.ai_keys, [prov]: r.ai_key }; inp.value = ""; toast("API key saved"); renderSettings(); }
      catch (e) { toast("Couldn't save the key"); }
    };
    wrap.append(inp, saveBtn);
    if (has) {
      const rm = document.createElement("button");
      rm.className = "btn small ghost"; rm.textContent = "Remove";
      rm.onclick = async () => { await api("ai-key", { remove: true, provider: prov }); S.ai_keys = { ...S.ai_keys, [prov]: false }; toast("API key removed"); renderSettings(); };
      wrap.append(rm);
    }
    const site = (S.ai_key_sites || {})[prov];
    const hint = has ? "Saved, encrypted for your Windows account." + (S.ai_status ? " " + S.ai_status : "")
                     : `Create one at ${site} (pay-as-you-go).`;
    const r = row(`${name} API key`, hint, wrap);
    $("small", r).classList.add("key-status");
    if (has) $("small", r).classList.add("ok");
    return r;
  }
  if (it.type === "startup") {
    const sw = document.createElement("button");
    sw.className = "switch" + (S.startup ? " on" : "");
    sw.onclick = async () => {
      try { const r = await api("startup", { on: !S.startup }); S.startup = r.startup; sw.classList.toggle("on", S.startup); }
      catch (e) { toast("Couldn't change that"); }
    };
    return row("Start when I log in to Windows", "Starts quietly in the tray", sw);
  }
  if (it.type === "cache") {
    const btn = document.createElement("button");
    btn.className = "btn small ghost"; btn.textContent = "Clear cache";
    const label = () => S.cache.count ? `${S.cache.count} songs, ${S.cache.kb.toLocaleString()} KB` : "Empty";
    const r = row("Lyrics cache", label(), btn);
    btn.onclick = async () => { S.cache = await api("cache/clear", {}); $("small", r).textContent = label(); toast("Cache cleared"); };
    return r;
  }
  if (it.type === "updates") {
    const btn = document.createElement("button");
    btn.className = "btn small ghost"; btn.textContent = "Check now";
    const r = row("Updates", "New versions come from GitHub", btn);
    const note = $("small", r);
    btn.onclick = async () => {
      btn.disabled = true; note.textContent = "Checking…";
      try {
        const res = await api("update/check", {});
        if (res.error) note.textContent = res.error;
        else if (res.latest) note.textContent = `You have the latest version (${(S.about || {}).version || ""}).`;
        else { note.textContent = `Version ${res.update.version} is available: see the notice at the top.`; updLaterFor = null; renderUpdate(res.update); }
      } catch (e) { note.textContent = "Couldn't check for updates."; }
      btn.disabled = false;
    };
    return r;
  }
  if (it.type === "cover") return coverEditor(s);
  if (it.type === "about") {
    const a = S.about || {};
    const frag = document.createElement("div");
    const where = a.installed ? "Installed on this PC"
      : a.packaged ? "Running from the downloaded folder (not installed)" : "Running from the source code";
    frag.append(row(`Autosynced Lyrics ${a.version || ""}`, where, null),
                row("Program folder", a.location || "", null),
                row("Your data", (a.data || "") + " (settings, synced lyrics, cache)", null));
    if (a.packaged) frag.append(updateRow());
    return frag;
  }
  return document.createElement("div");
}

// Settings -> About: install a small update (AutosyncedLyrics-Update.zip) over this copy.
function updateRow() {
  const pick = document.createElement("input");
  pick.type = "file"; pick.accept = ".zip"; pick.hidden = true;
  const btn = document.createElement("button");
  btn.className = "btn small ghost"; btn.textContent = "Update from file…";
  const wrap = document.createElement("div");
  wrap.append(pick, btn);
  const r = row("Update", "Choose AutosyncedLyrics-Update.zip. Only the app's own code is replaced; your data stays.", wrap);
  const note = $("small", r);
  btn.onclick = () => pick.click();
  pick.onchange = async () => {
    const f = pick.files[0];
    pick.value = "";
    if (!f) return;
    btn.disabled = true; note.textContent = "Updating…";
    try {
      const res = await fetch("/api/update", { method: "POST", headers: { "X-Token": TOKEN, "Content-Type": "application/zip" }, body: f });
      const d = await res.json().catch(() => ({}));
      if (d.ok) { note.textContent = `Updated to ${d.version || "the new version"}. Restarting…`; toast("Updated: restarting"); return; }
      note.textContent = d.error || "The update didn't work.";
    } catch (e) { note.textContent = "The update didn't work."; }
    btn.disabled = false;
  };
  return r;
}

function coverEditor(s) {
  const cfg = () => ({ source: S.settings.cover_source, shape: S.settings.cover_shape, motion: S.settings.cover_motion, speed: S.settings.cover_speed,
                       arm: S.settings.cover_arm, glow: S.settings.cover_glow, reflect: S.settings.cover_reflect });
  const box = document.createElement("div");
  box.className = "cover-editor";
  box.innerHTML = `<div class="cover-preview"><div class="record"></div></div><div class="cover-controls">
    <div class="row-label" style="margin-bottom:8px">Album cover<small>Pick a look, or mix your own below</small></div>
    <div class="presets"></div></div>`;
  const pv = buildCover($(".cover-preview .record", box));
  const controls = $(".cover-controls", box);

  const refresh = () => {
    const c = cfg();
    applyCover(pv, c, true);
    coverCfg = c;
    applyCover($("#record"), c, now.open && now.playing);
    $$(".chip", box).forEach(ch => {
      const p = COVER_PRESETS[ch.textContent];
      ch.classList.toggle("on", Object.keys(p).every(k => k === "speed" ? +p[k] === +c[k] : p[k] === c[k]));
    });
    $$(".seg", box).forEach(sg => $$("button", sg).forEach(b => b.classList.toggle("on", b.dataset.v === c[sg.dataset.k])));
    $$(".switch", box).forEach(sw => sw.classList.toggle("on", !!c[sw.dataset.k]));
    const sp = $("input[type=range]", box);
    sp.value = c.speed; setPct(sp); $(".val", box).textContent = `${c.speed} s`;
    $(".arm-row", box).classList.toggle("disabled", !["vinyl", "cd"].includes(c.shape) || c.source === "none");
    $$(".row", controls).forEach(r => { if (!r.classList.contains("source-row") && !r.classList.contains("arm-row"))
      r.classList.toggle("disabled", c.source === "none"); });
    $(".presets", box).classList.toggle("disabled-presets", c.source === "none");
    updateSurfaces();
  };
  const set = (changes) => {
    for (const [k, v] of Object.entries(changes)) save("cover_" + k, v);
    refresh();
  };

  for (const name of Object.keys(COVER_PRESETS)) {
    const chip = document.createElement("button");
    chip.className = "chip"; chip.textContent = name;
    chip.onclick = () => set(COVER_PRESETS[name]);
    $(".presets", box).appendChild(chip);
  }
  const seg = (key, label, options) => {
    const sg = document.createElement("div");
    sg.className = "seg"; sg.dataset.k = key;
    for (const [v, text] of options) {
      const b = document.createElement("button");
      b.dataset.v = v; b.textContent = text;
      b.onclick = () => set({ [key]: v });
      sg.appendChild(b);
    }
    controls.appendChild(row(label, "", sg));
  };
  seg("source", "Picture", [["album", "Album art"], ["gradient", "Colour mix"], ["none", "Hidden"]]);
  controls.lastElementChild.classList.add("source-row");
  seg("shape", "Shape", [["square", "Square"], ["rounded", "Rounded"], ["circle", "Circle"], ["vinyl", "Vinyl"], ["cd", "CD"], ["polaroid", "Polaroid"]]);
  seg("motion", "Motion", [["still", "Still"], ["spin", "Spin"], ["float", "Float"], ["sway", "Sway"], ["pulse", "Pulse"], ["tilt", "Tilt with mouse"]]);
  const sp = document.createElement("div");
  sp.className = "ctl";
  sp.innerHTML = `<input type="range" min="2" max="20" step="1"><span class="val"></span>`;
  $("input", sp).oninput = e => { setPct(e.target); $(".val", sp).textContent = `${e.target.value} s`; set({ speed: +e.target.value }); };
  controls.appendChild(row("Speed", "Seconds per turn or movement (lower is faster)", sp));
  for (const [key, label, hint] of [["arm", "Tonearm", "On vinyl and CD; lifts off when paused"],
                                    ["glow", "Glow", "A soft halo in the album's colour"],
                                    ["reflect", "Reflection", "A mirror image underneath"]]) {
    const sw = document.createElement("button");
    sw.className = "switch"; sw.dataset.k = key;
    sw.onclick = () => set({ [key]: !cfg()[key] });
    const r = row(label, hint, sw);
    if (key === "arm") r.classList.add("arm-row");
    controls.appendChild(r);
  }
  setTimeout(refresh);
  return box;
}

$("#btnReset").onclick = async () => {
  if (!(await confirmBox("Reset all settings?", "Every setting goes back to its default. Your saved lyrics, cache and API key are kept.", "Reset"))) return;
  await api("reset", {});
  setTimeout(loadSettings, 300);
  toast("Settings reset");
};

// ---------------------------------------------------------------- AI usage
async function loadUsage() {
  let u;
  try { u = await api("usage"); } catch (e) { return; }
  $("#stMonth").textContent = money(u.month.cost);
  $("#stMonthSongs").textContent = `${u.month.songs} request${u.month.songs === 1 ? "" : "s"}`;
  $("#stAll").textContent = money(u.all.cost);
  $("#stAllSongs").textContent = `${u.all.songs} request${u.all.songs === 1 ? "" : "s"}`;
  $("#stLines").textContent = u.all.lines.toLocaleString();
  const tb = $("#usageTable tbody");
  tb.innerHTML = "";
  $("#usageEmpty").hidden = u.rows.length > 0;
  for (const e of u.rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="muted">${esc(e.time)}</td><td class="title">${esc(e.title)} <span class="muted">— ${esc(e.artist)}</span></td>
      <td>${esc(e.model_name)}</td><td class="num">${e.lines}</td>
      <td class="num muted">${(e.in || 0).toLocaleString()} / ${(e.out || 0).toLocaleString()}</td><td class="num">${money(e.cost)}</td>`;
    tb.appendChild(tr);
  }
}
$("#usageRefresh").onclick = loadUsage;

// ---------------------------------------------------------------- start
buildCover($("#record"));
(async function start() {
  try { S = await api("settings"); applyStyle(S.settings.ui_style); setLowCpu(S.settings.low_cpu); } catch (e) {}
  let page = "now";
  try { page = localStorage.getItem("page") || "now"; } catch (e) {}
  showPage(["now", "library", "editor", "settings", "usage"].includes(page) ? page : "now");
  poll();
  tick(false);
})();
