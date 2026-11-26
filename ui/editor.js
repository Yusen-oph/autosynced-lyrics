// Editor page: write, find, tap-sync and AI-sync lyrics, then save them to My Lyrics.
// Uses helpers from app.js (api, $, $$, toast, confirmBox, esc, now, position).
"use strict";

const ED_STAMP = /^\s*((?:\[\d+:\d+(?:\.\d+)?\])+)\s*/;
const ED_ONE = /\[(\d+):(\d+(?:\.\d+)?)\]/;
const ED_TAG = /^\s*\[(ti|ar|al|length|by|offset|re|ve):.*\]\s*$/i;
const ED_UNSURE = /\[([^\[\]]+)\?\]/g;

const ed = {
  lines: [],            // [{ t: seconds | null, text, guess }]
  sel: null,            // selected line
  picked: new Set(),    // lines picked to move together (Shift-click a range, Ctrl-click one)
  syncing: false,
  target: null,         // line the next tap stamps
  history: [],          // tap undo: [{ i, t, guess }]
  raw: false,           // editing as plain text
  aiPolling: false,
};

const stamp = s => {
  s = Math.max(0, s);
  const m = Math.floor(s / 60), sec = (s - m * 60).toFixed(2).padStart(5, "0");
  return `[${String(m).padStart(2, "0")}:${sec}]`;
};

function edParse(text) {
  return String(text || "").replace(/\r/g, "").split("\n").filter(l => !ED_TAG.test(l)).map(l => {
    const m = l.match(ED_STAMP);
    if (!m) return { t: null, text: l.trim(), guess: false };
    const one = m[1].match(ED_ONE);
    return { t: +one[1] * 60 + parseFloat(one[2]), text: l.slice(m[0].length).trim(), guess: false };
  });
}
const edSerialize = () => ed.lines.map(l => (l.t != null ? stamp(l.t) : "") + l.text).join("\n");
const edSong = () => ({ title: $("#edTitle").value.trim(), artist: $("#edArtist").value.trim() });
const edStatus = text => { const el = $("#edStatus"); el.textContent = text || ""; el.title = text || ""; };
const isLyric = i => ed.lines[i] && ed.lines[i].text.trim() !== "";

function edNext(i, step = 1) {
  while (i >= 0 && i < ed.lines.length) { if (isLyric(i)) return i; i += step; }
  return null;
}

function edSetText(text) {
  ed.lines = edParse(text);
  if (!ed.lines.length) ed.lines = [{ t: null, text: "", guess: false }];
  ed.sel = null; ed.history = []; ed.picked.clear();
  if (ed.raw) $("#edRaw").value = edSerialize();
  edRender();
}

// ---------------------------------------------------------------- drawing
// One line's row (number, time chip, words, hover actions).
function edRowClass(l, i) {
  return "ed-line" + (l.t != null ? " done" : "") + (l.guess ? " guess" : "") + (!l.text ? " blank" : "")
    + (i === ed.sel ? " sel" : "") + (ed.picked.has(i) ? " picked" : "") + (ed.syncing && i === ed.target ? " target" : "");
}
const edTimeText = l => l.t != null ? stamp(l.t).slice(1, -1) : "--:--.--";

function edMakeRow(l, i) {
  const row = document.createElement("div");
  row.className = edRowClass(l, i);
  row.dataset.i = i;
  const words = l.text ? esc(l.text).replace(ED_UNSURE, '<span class="unsure">[$1?]</span>') : "(empty line)";
  row.innerHTML = `<span class="num">${i + 1}</span>
    <span class="time" title="Click to set this line's time to now">${edTimeText(l)}</span>
    <span class="words">${words}</span>
    <span class="acts">
      <button data-a="minus" title="0.1 s earlier">−0.1</button><button data-a="plus" title="0.1 s later">+0.1</button>
      <button data-a="clear" title="Remove the time">✕</button><button data-a="add" title="New line below">＋</button>
      <button data-a="del" title="Delete this line">🗑</button></span>`;
  return row;
}

// Redraw every row. The scroll position is kept exactly: emptying the list for a moment
// would otherwise make the browser reset it, so the list jumped.
function edRender() {
  const box = $("#edLines");
  const keep = box.scrollTop;
  const frag = document.createDocumentFragment();
  ed.lines.forEach((l, i) => frag.appendChild(edMakeRow(l, i)));
  box.replaceChildren(frag);
  box.style.scrollBehavior = "auto";           // put it back instantly, not with a smooth scroll
  box.scrollTop = keep;
  box.style.scrollBehavior = "";
  edStats();
}

// Update rows in place (time, highlight) without rebuilding the list: nothing moves.
function edRefresh(...indexes) {
  for (const i of indexes) {
    const row = $(`.ed-line[data-i="${i}"]`), l = ed.lines[i];
    if (!row || !l) continue;
    row.className = edRowClass(l, i);
    $(".time", row).textContent = edTimeText(l);
  }
  edStats();
}

function edStats() {
  const lyric = ed.lines.filter(l => l.text.trim());
  const synced = lyric.filter(l => l.t != null).length;
  const unsure = (edSerialize().match(ED_UNSURE) || []).length;
  $("#edCount").textContent = `${lyric.length} line${lyric.length === 1 ? "" : "s"}`;
  $("#edSynced").textContent = `${synced} / ${lyric.length}`;
  $("#edUnsure").textContent = unsure;
  $("#edProgressFill").style.width = lyric.length ? `${synced / lyric.length * 100}%` : "0";
  edPickInfo();
}

// ---------------------------------------------------------------- moving some lines together
// A verse timed for another version of the song: pick its lines (Shift-click the last one
// after choosing the first, Ctrl-click to add or drop one, or "to the end"), then move them
// with the shift buttons, or click a picked line's time while the song plays to move them all
// so that line starts now.
function edPickInfo() {
  const n = ed.picked.size;
  $("#edShiftLabel").textContent = n ? `Shift ${n} picked` : "Shift all";
  $("#edPickClear").hidden = !n;
  $("#edPickEnd").hidden = ed.sel == null;
}
function edPickRange(a, b) {
  const [lo, hi] = a < b ? [a, b] : [b, a];
  for (let i = lo; i <= hi; i++) if (isLyric(i)) ed.picked.add(i);
}
function edShiftLines(d, which) {
  for (const i of which) { const l = ed.lines[i]; if (l && l.t != null) l.t = Math.max(0, l.t + d); }
}
const edMoving = () => ed.picked.size ? [...ed.picked] : ed.lines.map((_, i) => i);
function edClearPicks() { const was = [...ed.picked]; ed.picked.clear(); edRefresh(...was); }

function edScrollTo(i) {
  const row = $(`.ed-line[data-i="${i}"]`);
  if (!row) return;
  const box = $("#edLines");
  box.scrollTo({ top: row.offsetTop - box.clientHeight * 0.35, behavior: "smooth" });
}

// ---------------------------------------------------------------- line interactions
$("#edLines").addEventListener("click", e => {
  const row = e.target.closest(".ed-line");
  if (!row) return;
  const i = +row.dataset.i, l = ed.lines[i];
  const act = e.target.closest("[data-a]");
  if (act) {
    const a = act.dataset.a;
    if (a === "minus" || a === "plus" || a === "clear") {     // just this line: nothing else moves
      if (a === "minus" && l.t != null) l.t = Math.max(0, l.t - 0.1);
      if (a === "plus" && l.t != null) l.t += 0.1;
      if (a === "clear") { l.t = null; l.guess = false; }
      edRefresh(i);
      return;
    }
    if (a === "add") { ed.lines.splice(i + 1, 0, { t: null, text: "", guess: false }); ed.sel = i + 1;
                       edRender(); edEditLine(i + 1); return; }
    if (a === "del") { ed.lines.splice(i, 1); if (!ed.lines.length) ed.lines.push({ t: null, text: "", guess: false }); }
    edRender();
    return;
  }
  if ((e.shiftKey || e.ctrlKey) && !ed.syncing) {          // pick lines to move together
    if (e.ctrlKey) { if (ed.picked.has(i)) ed.picked.delete(i); else ed.picked.add(i); }
    else edPickRange(ed.sel ?? i, i);
    const s = window.getSelection(); if (s) s.empty();       // no text highlight from Shift-click
    edRender();
    return;
  }
  if (e.target.closest(".time") && !ed.syncing) {        // stamp this line with "now"
    if (!now.open) return toast("Play the song first");
    const t = Math.max(0, position() - edDelay());
    if (ed.picked.has(i) && l.t != null) {                // the picked lines all move with it
      const d = t - l.t;
      edShiftLines(d, ed.picked);
      ed.picked.forEach(j => { ed.lines[j].guess = false; });
      edRender();
      edStatus(`${ed.picked.size} picked lines moved ${d > 0 ? "later" : "earlier"} by ${Math.abs(d).toFixed(2)} s`);
      return;
    }
    l.t = t; l.guess = false;
  }
  const before = [ed.sel, ed.target];
  ed.sel = i;
  if (ed.syncing) ed.target = isLyric(i) ? i : edNext(i);
  edRefresh(...new Set([...before, ed.sel, ed.target].filter(x => x != null)));
});
$("#edLines").addEventListener("dblclick", e => {
  const row = e.target.closest(".ed-line");
  if (row && !ed.syncing && e.target.closest(".words")) edEditLine(+row.dataset.i);
});

function edEditLine(i) {
  const row = $(`.ed-line[data-i="${i}"]`);
  if (!row) return;
  const cell = $(".words", row);
  const inp = document.createElement("input");
  inp.type = "text"; inp.value = ed.lines[i].text;
  cell.innerHTML = ""; cell.appendChild(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = keep => {
    if (done) return; done = true;
    if (keep) ed.lines[i].text = inp.value.trim();
    edRender();
  };
  inp.addEventListener("keydown", e => {
    e.stopPropagation();
    if (e.key === "Enter") { finish(true); if (e.shiftKey === false && i + 1 < ed.lines.length) {} }
    if (e.key === "Escape") finish(false);
  });
  inp.addEventListener("blur", () => finish(true));
}

// ---------------------------------------------------------------- tap-sync
const edDelay = () => Math.max(0, Math.min(1, parseFloat($("#edDelay").value) || 0));

function edStartSync() {
  if (ed.raw) edToggleRaw();
  if (!now.open) return toast("Start playing the song in your music app first");
  const from = ed.sel != null ? ed.sel : 0;
  ed.target = edNext(from) ?? edNext(0);
  if (ed.target == null) return toast("Add some lyric lines first");
  ed.syncing = true; ed.history = [];
  $("#edSync span").textContent = "Stop syncing";
  $("#edSync").classList.replace("primary", "ghost");
  edRender(); edScrollTo(ed.target);
}
function edStopSync(msg) {
  ed.syncing = false;
  $("#edSync span").textContent = "Start tap-sync";
  $("#edSync").classList.replace("ghost", "primary");
  edRender();
  if (msg) edStatus(msg);
}
function edTap() {
  if (!ed.syncing || ed.target == null) return;
  const l = ed.lines[ed.target];
  ed.history.push({ i: ed.target, t: l.t, guess: l.guess });
  l.t = Math.max(0, position() - edDelay()); l.guess = false;
  const btn = $("#edTap"); btn.classList.add("hit"); setTimeout(() => btn.classList.remove("hit"), 110);
  const nxt = edNext(ed.target + 1);
  if (nxt == null) return edStopSync("All lines synced. Save to use them.");
  ed.target = nxt;
  edRender(); edScrollTo(nxt);
}
function edUndo() {
  const h = ed.history.pop();
  if (!h) return;
  Object.assign(ed.lines[h.i], { t: h.t, guess: h.guess });
  ed.target = h.i;
  edRender(); edScrollTo(h.i);
}
$("#edSync").onclick = () => ed.syncing ? edStopSync() : edStartSync();
$("#edTap").onclick = edTap;
$("#edUndo").onclick = edUndo;

document.addEventListener("keydown", e => {
  if (!$("#page-editor").classList.contains("on")) return;
  const typing = e.target instanceof Element && e.target.matches("input, textarea, select");
  if (e.ctrlKey && (e.key === "r" || e.key === "R")) { e.preventDefault(); edReplay(e.shiftKey); return; }
  if (!ed.syncing && !typing && e.key === "Escape" && ed.picked.size) { e.preventDefault(); edClearPicks(); return; }
  if (!ed.syncing || typing) return;
  if (e.key === " " || e.key === "Enter") { e.preventDefault(); edTap(); }
  else if (e.key === "Backspace") { e.preventDefault(); edUndo(); }
  else if (e.key === "Escape") { e.preventDefault(); edStopSync(); }
});

// ---------------------------------------------------------------- playback helpers
const seekRaw = s => api("control", { name: "seek", to: Math.max(0, s), raw: true }).catch(() => toast("The music app didn't respond"));
$("#edRestart").onclick = () => seekRaw(0);
$("#edBack5").onclick = () => seekRaw(position() - 5);
$("#edPlay").onclick = () => api("control", { name: "play" });

function edLineWindow() {
  const row = ed.sel ?? ed.target ?? 0;
  let start = null;
  for (let i = Math.min(row, ed.lines.length - 1); i >= 0; i--) if (ed.lines[i].t != null) { start = ed.lines[i].t; break; }
  if (start == null) return null;
  let end = start + 6;
  for (let i = row + 1; i < ed.lines.length; i++) if (ed.lines[i].t != null && ed.lines[i].t > start) { end = ed.lines[i].t; break; }
  return [start, Math.min(end, start + 15)];
}
async function edReplay(slow) {
  const w = edLineWindow();
  if (!w) return edStatus("Choose a line that has a time first.");
  if (!slow) {
    if (!now.can_seek) return edStatus("Your music app doesn't allow jumping to a position.");
    await seekRaw(w[0] - 1);
    if (!now.playing) api("control", { name: "play" });
    return edStatus(`Replaying from ${stamp(w[0] - 1).slice(1, -1)}`);
  }
  try { const r = await api("editor/slow", { start: w[0], end: w[1], ...edSong() }); edStatus(r.message); }
  catch (e) { edStatus(e.message); }
}
$("#edReplay").onclick = () => edReplay(false);
$("#edSlow").onclick = () => edReplay(true);

// ---------------------------------------------------------------- bulk edits & raw text
$$("[data-shift]").forEach(b => b.onclick = () => {
  const d = parseFloat(b.dataset.shift);
  edShiftLines(d, edMoving());
  edRender();
  edStatus(`${ed.picked.size ? ed.picked.size + " picked lines" : "All times"} moved ${d > 0 ? "later" : "earlier"} by ${Math.abs(d)} s`);
});
$("#edPickClear").onclick = edClearPicks;
$("#edPickEnd").onclick = () => {                          // the chosen line and everything after it
  if (ed.sel == null) return;
  edPickRange(ed.sel, ed.lines.length - 1);
  edRender();
};
$("#edClear").onclick = async () => {
  if (await confirmBox("Clear all times?", "Every line loses its timing; the words stay.", "Clear")) {
    ed.lines.forEach(l => { l.t = null; l.guess = false; }); edRender();
  }
};
function edToggleRaw() {
  ed.raw = !ed.raw;
  if (ed.raw) { $("#edRaw").value = edSerialize(); }
  else { const keepGuess = ed.lines; ed.lines = edParse($("#edRaw").value); if (!ed.lines.length) ed.lines = [{ t: null, text: "", guess: false }]; }
  $("#edRaw").hidden = !ed.raw;
  $("#edLines").hidden = ed.raw;
  $("#edHint").hidden = ed.raw;
  $("#edRawBtn").textContent = ed.raw ? "Done editing text" : "Edit as text";
  edRender();
  if (ed.raw) $("#edRaw").focus();
}
$("#edRawBtn").onclick = () => { if (ed.syncing) edStopSync(); edToggleRaw(); };
$("#edRaw").addEventListener("input", () => { ed.lines = edParse($("#edRaw").value); edStats(); });

// ---------------------------------------------------------------- song, search, save
async function edLoad() {
  const s = edSong();
  if (!s.title) return;
  try {
    const r = await api(`editor/load?title=${encodeURIComponent(s.title)}&artist=${encodeURIComponent(s.artist)}`);
    if (r.saved) { edSetText(r.text); edStatus("Loaded your saved version."); }
    else { edSetText(""); edSearch(); }
  } catch (e) { edStatus(e.message); }
}
async function edSearch() {
  const s = edSong();
  if (!s.title) return toast("Enter a title first");
  edStatus("Searching online…");
  let r;
  try { r = await api("editor/search", s); } catch (e) { return edStatus(e.message); }
  if (r.error) return edStatus(r.error);
  if (!r.found) return edStatus("No lyrics found online. Paste or type them (Edit as text), then tap-sync.");
  const hasText = ed.lines.some(l => l.text.trim());
  if (hasText && !(await confirmBox("Replace the lyrics?", "Replace what's in the editor with the lyrics found online?", "Replace"))) return;
  edSetText(r.text);
  edStatus(`Found ${r.synced ? "synced" : "untimed"} lyrics (${r.source}): ${r.track} — ${r.artist}. ` +
           (r.synced ? "Check them, adjust, then Save." : "Play the song and use tap-sync."));
}
function edUseCurrent() {
  if (!now.open) return toast("Nothing is playing");
  $("#edTitle").value = now.title; $("#edArtist").value = now.artist;
  edLoad();
}
$("#edUseCurrent").onclick = edUseCurrent;
$("#edSearch").onclick = edSearch;

$("#edSave").onclick = async () => {
  if (ed.raw) ed.lines = edParse($("#edRaw").value);
  const s = edSong(), text = edSerialize();
  if (!s.title || !text.trim()) return toast("Enter a title and some lyrics first");
  const unsure = (text.match(ED_UNSURE) || []).length;
  if (unsure && !(await confirmBox("Unclear words left", `${unsure} word(s) are still marked [like this?]. They'll show on the lyrics bar as they are. Save anyway?`, "Save"))) return;
  try {
    const r = await api("editor/save", { ...s, text });
    toast("Saved");
    edStatus(`Saved (${r.synced ? "synced" : "no timing yet"}) as ${r.file}`);
  } catch (e) { edStatus(e.message); }
};
$("#edDelete").onclick = async () => {
  const s = edSong();
  if (!s.title) return;
  if (!(await confirmBox("Delete your version?", `Your saved lyrics for "${s.title}" will be deleted. The song will use lyrics found online again.`))) return;
  const r = await api("editor/delete", s);
  edStatus(r.deleted ? "Deleted. The lyrics bar uses online lyrics again." : "You have no saved version of this song.");
};

// ---------------------------------------------------------------- AI listening
async function edAiStart(mode) {
  const s = edSong();
  const words = ed.lines.map(l => l.text);
  if (mode === "align" && !words.some(w => w.trim()))
    return confirmBox("No lyrics yet", "Search online or paste the lyrics first; they don't need timing. No lyrics anywhere? Use Transcribe instead.", "OK");
  if (mode === "transcribe" && words.some(w => w.trim()) &&
      !(await confirmBox("Replace the lyrics?", "Transcribe writes a new draft and replaces what's in the editor.", "Continue"))) return;
  if (!now.open) return toast("Open the song in your music app first");
  if (s.title && (now.title !== s.title || now.artist !== s.artist) &&
      !(await confirmBox("Different song", `${now.player} is playing "${now.title}" by ${now.artist}, not the song in the editor. Continue anyway?`, "Continue"))) return;
  const restart = $("#edAiRestart").classList.contains("on"), model = $("#edModel").value;
  if (!(await confirmBox("Listen to the song", `The app will listen to your speakers ${restart ? "from the start of the song " : ""}until it ends (or you press Finish), then ${mode === "align" ? "line up the lyrics" : "write a timed draft"}. Keep other sounds quiet and don't skip around.`, "Start"))) return;
  if (ed.syncing) edStopSync();
  try { await api("editor/ai/start", { mode, lines: words, model, restart, ...s }); }
  catch (e) { return toast(e.message); }
  edAiPoll();
}
async function edAiPoll() {
  if (ed.aiPolling) return;
  ed.aiPolling = true;
  const panel = $("#edAiStatus"), btns = [$("#edAiAlign"), $("#edAiTrans")];
  try {
    for (;;) {
      const st = await api("editor/ai");
      const busy = st.state === "recording" || st.state === "processing";
      panel.hidden = !busy;
      btns.forEach(b => b.disabled = busy);
      $("#edAiFinish").hidden = st.state !== "recording";
      if (st.state === "recording") {
        $("#edAiMsg").textContent = `Listening… ${Math.round(st.seconds)} s recorded`;
        $("#edAiFill").style.width = now.duration ? `${Math.min(100, st.seconds / now.duration * 100)}%` : "30%";
      } else if (st.state === "processing") {
        $("#edAiMsg").textContent = `${st.message || "Working…"} ${st.progress ? Math.round(st.progress * 100) + "%" : ""}`;
        $("#edAiFill").style.width = `${Math.round(st.progress * 100)}%`;
      }
      if (st.result) { edAiDone(st.result); break; }
      if (st.state === "error") { edStatus(st.message); toast("AI listening failed"); await api("editor/ai/cancel", {}); break; }
      if (!busy) break;
      await new Promise(r => setTimeout(r, 500));
    }
  } finally {
    ed.aiPolling = false;
    $("#edAiStatus").hidden = true;
    [$("#edAiAlign"), $("#edAiTrans")].forEach(b => b.disabled = false);
  }
}
function edAiDone(res) {
  ed.lines = res.lines.map(l => ({ t: l.t, text: l.text, guess: !!l.guess }));
  if (!ed.lines.length) return edStatus("No singing was recognised in the recording.");
  edRender();
  const lyric = ed.lines.filter(l => l.text.trim());
  if (res.mode === "align") {
    const guessed = lyric.filter(l => l.guess).length;
    edStatus(`Auto-synced ${lyric.length - guessed}/${lyric.length} lines (${res.reason}). Orange = guessed: play it back, fix, then Save.`);
  } else {
    const unsure = (edSerialize().match(ED_UNSURE) || []).length;
    edStatus(`Draft: ${lyric.length} lines, ${unsure} unclear words in red (${res.reason}). Choose a line and Ctrl+R to hear it again; fix, then Save.`);
  }
}
$("#edAiAlign").onclick = () => edAiStart("align");
$("#edAiTrans").onclick = () => edAiStart("transcribe");
$("#edAiFinish").onclick = () => api("editor/ai/finish", {});
$("#edAiRestart").onclick = e => e.currentTarget.classList.toggle("on");

async function edInitModels() {
  try {
    const st = await api("editor/ai");
    const sel = $("#edModel");
    sel.innerHTML = st.models.map(m => `<option value="${m.name}">${m.name}${m.downloaded ? " ✓" : ""} — ${esc(m.info.split(",")[0])}</option>`).join("");
    const have = st.models.find(m => m.name === "small" && m.downloaded) || st.models.find(m => m.downloaded);
    sel.value = have ? have.name : "small";
    if (st.state === "recording" || st.state === "processing") edAiPoll();
  } catch (e) {}
}

// ---------------------------------------------------------------- live line & opening
function edLive() {
  if ($("#page-editor").classList.contains("on")) {
    const live = $("#edLive");
    if (!now.open) live.textContent = "Nothing playing. Start the song in your music app.";
    else {
      const p = position();
      const lyric = ed.lines.filter(l => l.text.trim()).length;
      live.innerHTML = `${now.playing ? "▶" : "⏸"} <b>${esc(now.title)}</b> — ${esc(now.artist)} · ${stamp(p).slice(1, -1)} · ${esc(now.player)}` +
        (ed.syncing && ed.target != null ? ` · <b>syncing line ${ed.target + 1} of ${ed.lines.length}</b>` : "");
      // which line is playing now (by its time)
      let cur = -1;
      ed.lines.forEach((l, i) => { if (l.t != null && l.t <= p) cur = i; });
      $$(".ed-line").forEach(r => r.classList.toggle("playing-now", +r.dataset.i === cur));
    }
    $("#edTap").disabled = !ed.syncing;
  }
  setTimeout(edLive, 150);
}

function openEditor(title, artist) {
  showPage("editor");
  if (title) {
    $("#edTitle").value = title; $("#edArtist").value = artist || "";
    edLoad();
  } else if (!$("#edTitle").value && now.open) {
    edUseCurrent();
  }
}

edSetText("");
edInitModels();
edLive();
