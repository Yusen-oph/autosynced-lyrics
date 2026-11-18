"""Write / find / tap-sync lyrics for a song and save them to my_lyrics/*.lrc."""
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import requests

import autosync
import lyrics as lyrics_api
import theme

_STAMP = re.compile(r"^\s*((?:\[\d+:\d+(?:\.\d+)?\])+)\s*")
_ONE_STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")

HELP = ("Tap-sync: play the song in your music app, click a line (or start at the top) and press "
        "Start. Press Space / Enter the moment each line starts. Backspace = undo, Esc = stop.")


def fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"[{int(seconds // 60):02d}:{seconds % 60:05.2f}]"


def _section(parent, title: str, top: int = 0):
    """A bold heading with its controls under it (plain frames blend with the theme)."""
    ttk.Label(parent, text=title, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(top, 2))
    body = ttk.Frame(parent, padding=(8, 0, 0, 0))
    body.pack(fill="x")
    return body


class LyricsEditor:
    def __init__(self, master, watcher, on_saved):
        self.watcher = watcher
        self.on_saved = on_saved
        self.syncing = False
        self.target = 0          # line number (0-based) that the next tap stamps
        self._ui = queue.Queue()   # results from worker threads, handled on the Tk thread
        self.recorder: autosync.Recorder | None = None
        self.ai_state = None       # None | "recording" | "processing"
        self.ai_mode = "align"     # "align" (time lyrics you have) | "transcribe" (write a draft)
        self.slow_playing = False

        self.win = tk.Toplevel(master)
        self.win.title("Lyrics editor")
        theme.title_bar(self.win)
        self.win.attributes("-topmost", True)
        root = ttk.Frame(self.win, padding=12)
        root.pack(fill="both", expand=True)

        # song
        song = ttk.Frame(root)
        song.pack(fill="x")
        self.title_var, self.artist_var = tk.StringVar(), tk.StringVar()
        ttk.Label(song, text="Title").grid(row=0, column=0, sticky="w")
        ttk.Entry(song, textvariable=self.title_var).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(song, text="Artist").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(song, textvariable=self.artist_var).grid(row=1, column=1, sticky="we", padx=6,
                                                            pady=(4, 0))
        ttk.Button(song, text="Use current song", command=self.use_current).grid(row=0, column=2)
        ttk.Button(song, text="Search online", command=self.search).grid(row=1, column=2, pady=(4, 0))
        song.columnconfigure(1, weight=1)

        # text
        box = ttk.Frame(root)
        box.pack(fill="both", expand=True, pady=10)
        self.text = tk.Text(box, wrap="word", undo=True, font=("Segoe UI", 11), padx=8, pady=6,
                            width=60, height=14, relief="flat", borderwidth=0,
                            background=theme.TEXT["bg"], foreground=theme.TEXT["fg"],
                            insertbackground=theme.TEXT["insert"],
                            selectbackground=theme.TEXT["select"])
        sb = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, look in theme.TAGS.items():
            self.text.tag_configure(tag, **look)

        # sync controls
        sync = _section(root, "Sync")
        ttk.Label(sync, text=HELP, wraplength=560, foreground=theme.MUTED).pack(fill="x", pady=(0, 6))
        row = ttk.Frame(sync)
        row.pack(fill="x")
        self.sync_btn = ttk.Button(row, text="▶ Start tap-sync", command=self.toggle_sync)
        self.sync_btn.pack(side="left")
        ttk.Button(row, text="TAP", width=6, command=self.tap).pack(side="left", padx=4)
        ttk.Button(row, text="Undo", command=self.undo).pack(side="left")
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(row, text="⏮ Restart song", command=self.watcher.restart).pack(side="left")
        ttk.Button(row, text="−5 s", width=5, command=lambda: self.jump(-5)).pack(side="left", padx=2)
        ttk.Button(row, text="⏯", width=3, command=self.watcher.toggle_play).pack(side="left")
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(row, text="⟲ Replay line", command=self.replay_line).pack(side="left")
        ttk.Button(row, text="🐢 Slow (75%)", command=lambda: self.replay_line(slow=True)).pack(
            side="left", padx=2)

        row2 = ttk.Frame(sync)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Reaction delay").pack(side="left")
        self.delay_var = tk.DoubleVar(value=0.2)
        ttk.Spinbox(row2, from_=0, to=1, increment=0.05, width=5,
                    textvariable=self.delay_var).pack(side="left", padx=(4, 2))
        ttk.Label(row2, text="s").pack(side="left")
        ttk.Separator(row2, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(row2, text="Shift all").pack(side="left")
        for d in (-0.5, -0.1, 0.1, 0.5):
            ttk.Button(row2, text=f"{d:+.1f}", width=5,
                       command=lambda d=d: self.shift_all(d)).pack(side="left", padx=1)
        ttk.Button(row2, text="Clear times", command=self.clear_times).pack(side="right")

        # AI auto-sync
        ai = _section(root, "AI (listens to the song)", top=8)
        ttk.Label(ai, wraplength=580, foreground=theme.MUTED, text=(
            "Auto-sync: have the lyrics in the box, the app times each line (orange = guessed). "
            "Transcribe: no lyrics anywhere? The app writes a synced draft from what it hears; "
            "words it isn't sure of are marked [like this?] in red. Click a line and use "
            "Replay / Slow (Ctrl+R / Ctrl+Shift+R) to hear it again.")).pack(fill="x", pady=(0, 6))
        arow = ttk.Frame(ai)
        arow.pack(fill="x")
        self.ai_btn = ttk.Button(arow, text="🤖 Listen & auto-sync", command=self.ai_button)
        self.ai_btn.pack(side="left")
        self.tr_btn = ttk.Button(arow, text="📝 Transcribe from audio",
                                 command=lambda: self.ai_button("transcribe"))
        self.tr_btn.pack(side="left", padx=(4, 0))
        self.restart_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(arow, text="Restart song first", variable=self.restart_var).pack(side="left", padx=8)
        ttk.Label(arow, text="Model").pack(side="left", padx=(8, 4))
        self.model_var = tk.StringVar(value="small")
        ttk.Combobox(arow, textvariable=self.model_var, values=list(autosync.MODEL_SIZES),
                     state="readonly", width=7).pack(side="left")
        self.model_hint = ttk.Label(arow, foreground=theme.MUTED, text=autosync.MODEL_SIZES["small"])
        self.model_hint.pack(side="left", padx=6)
        self.model_var.trace_add("write", lambda *_: self.model_hint.configure(
            text=autosync.MODEL_SIZES[self.model_var.get()]))

        self.live = ttk.Label(root, text="", font=("Segoe UI", 10, "bold"))
        self.live.pack(fill="x", pady=(8, 0))
        self.status = ttk.Label(root, text="", foreground=theme.MUTED)
        self.status.pack(fill="x", pady=(2, 4))

        bottom = ttk.Frame(root)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Save", command=self.save).pack(side="right")
        ttk.Button(bottom, text="Delete my version", command=self.delete).pack(side="right", padx=6)
        ttk.Button(bottom, text="Close", command=self.win.destroy).pack(side="left")

        for key in ("<space>", "<Return>"):
            self.text.bind(key, self._key_tap)
        self.text.bind("<BackSpace>", self._key_undo)
        self.text.bind("<Escape>", lambda e: self.stop_sync() or "break")
        self.text.bind("<KeyRelease>", lambda e: self._mark_unsure())
        self.win.bind("<Control-r>", lambda e: self.replay_line() or "break")
        self.win.bind("<Control-R>", lambda e: self.replay_line(slow=True) or "break")

        self.use_current(load=True)
        self._update_status()

    # ---- helpers ----------------------------------------------------------
    def exists(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def focus(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _set_text(self, text: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self._mark()

    def _lines(self) -> list[str]:
        return self.text.get("1.0", "end-1c").split("\n")

    def _replace_line(self, i: int, content: str):
        self.text.configure(state="normal")
        self.text.delete(f"{i + 1}.0", f"{i + 1}.end")
        self.text.insert(f"{i + 1}.0", content)
        if self.syncing:
            self.text.configure(state="disabled")

    def _next_lyric(self, i: int, step: int = 1) -> int | None:
        lines = self._lines()
        while 0 <= i < len(lines):
            if _STAMP.sub("", lines[i]).strip():
                return i
            i += step
        return None

    def _mark(self):
        self.text.tag_remove("target", "1.0", "end")
        self.text.tag_remove("done", "1.0", "end")
        for n, line in enumerate(self._lines()):
            if _STAMP.match(line):
                self.text.tag_add("done", f"{n + 1}.0", f"{n + 1}.end")
        if self.syncing and self.target is not None:
            self.text.tag_add("target", f"{self.target + 1}.0", f"{self.target + 1}.end")
            self.text.see(f"{self.target + 1}.0")
        self._mark_unsure()

    def _mark_unsure(self):
        """Highlight [word?] marks left by Transcribe."""
        self.text.tag_remove("unsure", "1.0", "end")
        for n, line in enumerate(self._lines()):
            for m in autosync._UNSURE.finditer(line):
                self.text.tag_add("unsure", f"{n + 1}.{m.start()}", f"{n + 1}.{m.end()}")

    def _current_track(self):
        t = self.watcher.track
        if t and t.title == self.title_var.get().strip() and t.artist == self.artist_var.get().strip():
            return t
        return None

    def _update_status(self):
        if not self.exists():
            return
        self._drain_ui()
        t = self.watcher.track
        if t is None:
            msg = "No music app is playing anything."
        else:
            pos = t.current_position()
            state = "▶" if t.playing else "⏸"
            msg = f"{state} {t.title} — {t.artist}   {fmt(pos)[1:-1]}   ({t.player})"
            if self.ai_state == "recording" and self.recorder:
                msg += f"   ·   🎙 listening, {self.recorder.seconds:.0f} s recorded"
            if self.syncing:
                total = sum(1 for l in self._lines() if l.strip())
                msg += f"   ·   syncing line {self.target + 1 if self.target is not None else '—'} / {total}"
        self.live.configure(text=msg)
        self.win.after(100, self._update_status)

    # ---- song / search ----------------------------------------------------
    def open_song(self, title: str, artist: str):
        """Show a particular saved song (from the app window's My Lyrics page)."""
        self.title_var.set(title)
        self.artist_var.set(artist)
        mine = lyrics_api.load_user_text(title, artist)
        if mine is not None:
            self._set_text(mine)
            self.status.configure(text="Loaded your saved version.")

    def use_current(self, load: bool = True):
        t = self.watcher.track
        if t is None:
            return
        self.title_var.set(t.title)
        self.artist_var.set(t.artist)
        if not load:
            return
        mine = lyrics_api.load_user_text(t.title, t.artist)
        if mine is not None:
            self._set_text(mine)
            self.status.configure(text="Loaded your saved version.")
        else:
            self._set_text("")
            self.search()

    def search(self):
        title, artist = self.title_var.get().strip(), self.artist_var.get().strip()
        if not title:
            return
        t = self._current_track()
        self.status.configure(text="Searching LRCLIB…")

        def work():
            try:
                rec = lyrics_api.search_online(title, artist, t.duration if t else 0, getattr(t, "album", "") if t else "")
                err = None
            except requests.RequestException:
                rec, err = None, "Network error, unable to fetch lyrics"
            except Exception as exc:
                rec, err = None, str(exc)
            self._ui.put(("search", rec, err))
        threading.Thread(target=work, daemon=True).start()

    def _search_done(self, rec, err):
        if not self.exists():
            return
        if err:
            messagebox.showerror("Search failed", err, parent=self.win)
            return
        if not rec:
            messagebox.showinfo("Nothing found", "No lyrics found online. Paste or type them in, "
                                "one line per lyric line, then tap-sync.", parent=self.win)
            return
        if self.text.get("1.0", "end-1c").strip() and not messagebox.askyesno(
                "Replace text?", "Replace what's in the editor with the lyrics found online?",
                parent=self.win):
            return
        if rec.get("syncedLyrics"):
            self._set_text(rec["syncedLyrics"])
            note = "Found synced lyrics. Check them, adjust, then Save."
        else:
            self._set_text(rec["plainLyrics"])
            note = "Found lyrics without timing. Play the song and use tap-sync."
        messagebox.showinfo("Found", f"{rec.get('trackName')} — {rec.get('artistName')}\n"
                            f"Source: {rec.get('source', 'LRCLIB')}\n\n{note}",
                            parent=self.win)

    # ---- tap sync ---------------------------------------------------------
    def toggle_sync(self):
        self.stop_sync() if self.syncing else self.start_sync()

    def start_sync(self):
        if self.watcher.track is None:
            messagebox.showinfo("Music app", "Start playing the song in your music app first.",
                                parent=self.win)
            return
        cursor_line = int(self.text.index("insert").split(".")[0]) - 1
        self.target = self._next_lyric(cursor_line) if cursor_line > 0 else self._next_lyric(0)
        if self.target is None:
            self.target = self._next_lyric(0)
        if self.target is None:
            messagebox.showinfo("No lyrics", "Add some lyric lines first.", parent=self.win)
            return
        self.syncing = True
        self.text.configure(state="disabled")
        self.sync_btn.configure(text="■ Stop syncing")
        self.text.focus_set()
        self._mark()

    def stop_sync(self):
        self.syncing = False
        self.text.configure(state="normal")
        self.sync_btn.configure(text="▶ Start tap-sync")
        self._mark()

    def tap(self):
        t = self.watcher.track
        if not self.syncing or self.target is None or t is None:
            return
        try:
            delay = float(self.delay_var.get())
        except (ValueError, tk.TclError):
            delay = 0.0
        words = _STAMP.sub("", self._lines()[self.target]).strip()
        self._replace_line(self.target, fmt(t.current_position() - delay) + words)
        self.text.tag_remove("guess", f"{self.target + 1}.0", f"{self.target + 1}.end")
        nxt = self._next_lyric(self.target + 1)
        if nxt is None:
            self.stop_sync()
            self.status.configure(text="All lines synced. Save to use them.")
            return
        self.target = nxt
        self._mark()

    def undo(self):
        if not self.syncing or self.target is None:
            return
        prev = self._next_lyric(self.target - 1, step=-1)
        if prev is None:
            return
        self._replace_line(prev, _STAMP.sub("", self._lines()[prev]))
        self.target = prev
        self._mark()

    def _key_tap(self, _):
        if self.syncing:
            self.tap()
            return "break"

    def _key_undo(self, _):
        if self.syncing:
            self.undo()
            return "break"

    def jump(self, delta: float):
        t = self.watcher.track
        if t:
            self.watcher.seek(t.current_position() + delta)

    # ---- AI auto-sync -----------------------------------------------------
    def ai_button(self, mode: str = "align"):
        if self.ai_state is None:
            self.ai_start(mode)
        elif self.ai_state == "recording" and self.recorder:
            self.recorder.stop("finished early")

    def ai_start(self, mode: str = "align"):
        plain = [_STAMP.sub("", l) for l in self._lines()]
        has_text = any(l.strip() for l in plain)
        if mode == "align" and not has_text:
            messagebox.showinfo("No lyrics", "Search online or paste the lyrics first. "
                                "They don't need any timing.\n\nNo lyrics anywhere? Use "
                                "“Transcribe from audio” instead.", parent=self.win)
            return
        if mode == "transcribe" and has_text and not messagebox.askyesno(
                "Replace text?", "Transcribe writes a new draft and replaces what's in the box. "
                "Continue?", parent=self.win):
            return
        t = self.watcher.track
        if t is None:
            messagebox.showinfo("Music app", "Open the song in your music app first.",
                                parent=self.win)
            return
        if (t.title, t.artist) != (self.title_var.get().strip(), self.artist_var.get().strip()):
            if not messagebox.askyesno("Different song", f"{t.player} is playing “{t.title}” by "
                                       f"{t.artist}, not the song in the editor. Continue anyway?",
                                       parent=self.win):
                return
        restart = self.restart_var.get()
        model = self.model_var.get()
        goal = "line up the lyrics" if mode == "align" else "write a synced draft of the lyrics"
        if not messagebox.askokcancel("Listen to the song", (
                "The app will listen to your speakers "
                + ("from the start of the song " if restart else "")
                + f"until it ends (or you press Finish), then {goal}.\n\n"
                "Keep other sounds quiet and don't pause or skip around. "
                f"The first run downloads the “{model}” speech model "
                f"({autosync.MODEL_SIZES[model].split(',')[0]})."), parent=self.win):
            return
        self.stop_sync()
        self.ai_mode = mode
        self.recorder = autosync.Recorder(self.watcher)
        self.recorder.song = (t.title, t.artist)
        if restart:
            self.watcher.restart()
            if not t.playing:
                self.watcher.toggle_play()
            self.win.after(800, self.recorder.start)   # let the seek land first
        else:
            self.recorder.start()
        self.ai_state = "recording"
        active, other = (self.ai_btn, self.tr_btn) if mode == "align" else (self.tr_btn, self.ai_btn)
        active.configure(text="■ Finish listening now")
        other.configure(state="disabled")
        self.status.configure(text="Listening… let the song play.")
        self._ai_wait()

    def _ai_wait(self):
        if not self.exists() or self.ai_state != "recording":
            return
        r = self.recorder
        if r.stop_flag.is_set() and not r.is_alive():
            self._ai_process()
        else:
            self.win.after(200, self._ai_wait)

    def _ai_process(self):
        r = self.recorder
        if r.error or r.seconds < 5:
            self._ai_reset()
            messagebox.showerror("AI auto-sync", r.error or "The recording was too short.",
                                 parent=self.win)
            return
        self.ai_state = "processing"
        for b in (self.ai_btn, self.tr_btn):
            b.configure(state="disabled")
        (self.ai_btn if self.ai_mode == "align" else self.tr_btn).configure(text="Working…")
        lines = [_STAMP.sub("", l).rstrip() for l in self._lines()]
        model, mode = self.model_var.get(), self.ai_mode

        def work():
            try:
                words = autosync.transcribe(r.audio_16k(), model,
                                            lambda f, m: self._ui.put(("progress", f, m)))
                if mode == "align":
                    times, guessed = autosync.align(lines, words, r.song_time)
                    self._ui.put(("ai_done", lines, times, guessed, r.stop_reason))
                else:
                    self._ui.put(("draft_done", autosync.draft_lyrics(words, r.song_time),
                                  r.stop_reason))
            except Exception as exc:
                self._ui.put(("ai_error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _ai_reset(self):
        self.ai_state = None
        self.ai_btn.configure(text="🤖 Listen & auto-sync", state="normal")
        self.tr_btn.configure(text="📝 Transcribe from audio", state="normal")

    def _draft_done(self, lines, reason):
        self._ai_reset()
        if not lines:
            messagebox.showinfo("Transcribe", "No singing was recognised in the recording.",
                                parent=self.win)
            return
        self._set_text("\n".join(fmt(t) + text for t, text in lines))
        unsure = autosync.count_unsure(self.text.get("1.0", "end-1c"))
        self.status.configure(text=f"Draft: {len(lines)} lines, {unsure} unclear words in red "
                                   f"({reason}). Click a line, Ctrl+R to hear it again; fix, then Save.")

    # ---- listen again -----------------------------------------------------
    def _line_window(self) -> tuple[float, float] | None:
        """Start time of the line with the cursor (or the stamped line above), and the next start."""
        lines = self._lines()
        row = int(self.text.index("insert").split(".")[0]) - 1
        times = []
        for l in lines:
            m = _ONE_STAMP.match(l.strip())
            times.append(int(m.group(1)) * 60 + float(m.group(2)) if m else None)
        start = next((times[i] for i in range(min(row, len(times) - 1), -1, -1) if times[i] is not None), None)
        if start is None:
            return None
        end = next((t for t in times[row + 1:] if t is not None and t > start), start + 6)
        return start, min(end, start + 15)

    def replay_line(self, slow: bool = False):
        window = self._line_window()
        if window is None:
            self.status.configure(text="Click a line that has a time stamp first.")
            return
        start, end = window
        if not slow:
            if not self.watcher.can_seek:
                self.status.configure(text="Your music app doesn't allow jumping to a position.")
                return
            self.watcher.seek(max(0.0, start - 1.0))
            t = self.watcher.track
            if t and not t.playing:
                self.watcher.toggle_play()
            self.status.configure(text=f"Replaying from {fmt(start - 1.0)[1:-1]}")
            return
        r = self.recorder
        song = (self.title_var.get().strip(), self.artist_var.get().strip())
        if r is None or getattr(r, "song", None) != song or r.is_alive():
            self.status.configure(text="Slow replay uses the app's recording: run Transcribe or "
                                       "Auto-sync on this song first.")
            return
        if self.slow_playing:
            return
        piece = r.snippet_16k(max(0.0, start - 0.3), end)
        if piece is None:
            self.status.configure(text="That part of the song wasn't recorded.")
            return
        t = self.watcher.track
        if t and t.playing:
            self.watcher.toggle_play()   # pause the music so the two don't overlap
        self.slow_playing = True
        self.status.configure(text=f"Playing {fmt(start)[1:-1]}–{fmt(end)[1:-1]} at 75% speed…")

        def work():
            try:
                autosync.play_audio(autosync.time_stretch(piece, 0.75))
            finally:
                self._ui.put(("slow_done",))
        threading.Thread(target=work, daemon=True).start()

    def _ai_done(self, lines, times, guessed, reason):
        self._ai_reset()
        out = [fmt(t) + l.strip() if t is not None else l for l, t in zip(lines, times)]
        self._set_text("\n".join(out))
        n_lines = sum(1 for l in lines if l.strip())
        n_guess = sum(1 for l, g in zip(lines, guessed) if l.strip() and g)
        for i, g in enumerate(guessed):
            if g:
                self.text.tag_add("guess", f"{i + 1}.0", f"{i + 1}.end")
        self.status.configure(text=f"Auto-synced {n_lines - n_guess}/{n_lines} lines from audio "
                                   f"({reason}). Orange = guessed. Play it back, fix, then Save.")

    def _drain_ui(self):
        while True:
            try:
                kind, *args = self._ui.get_nowait()
            except queue.Empty:
                return
            if kind == "search":
                self._search_done(*args)
            elif kind == "progress":
                frac, msg = args
                self.status.configure(text=f"{msg} {round(frac * 100)}%" if frac else msg)
            elif kind == "ai_done":
                self._ai_done(*args)
            elif kind == "draft_done":
                self._draft_done(*args)
            elif kind == "slow_done":
                self.slow_playing = False
                self.status.configure(text="Slow replay finished.")
            elif kind == "ai_error":
                self._ai_reset()
                messagebox.showerror("AI auto-sync failed", args[0], parent=self.win)

    # ---- bulk edits -------------------------------------------------------
    def shift_all(self, delta: float):
        def shift(m):
            return fmt(int(m.group(1)) * 60 + float(m.group(2)) + delta)
        lines = [_ONE_STAMP.sub(shift, l) for l in self._lines()]
        self._set_text("\n".join(lines))

    def clear_times(self):
        self._set_text("\n".join(_STAMP.sub("", l) for l in self._lines()))

    # ---- save -------------------------------------------------------------
    def save(self):
        title, artist = self.title_var.get().strip(), self.artist_var.get().strip()
        text = self.text.get("1.0", "end-1c")
        if not title or not text.strip():
            messagebox.showinfo("Missing info", "Enter a title and some lyrics first.", parent=self.win)
            return
        unsure = autosync.count_unsure(text)
        if unsure and not messagebox.askyesno(
                "Unclear words left", f"{unsure} word(s) are still marked [like this?]. "
                "They'll show on the lyrics bar as they are. Save anyway?", parent=self.win):
            return
        t = self._current_track()
        path = lyrics_api.save_user(title, artist, text, t.album if t else "", t.duration if t else 0)
        synced = lyrics_api.from_text(text)
        kind = "synced" if synced and synced.synced else "unsynced"
        self.status.configure(text=f"Saved ({kind}) to {path.name}")
        self.on_saved()

    def delete(self):
        title, artist = self.title_var.get().strip(), self.artist_var.get().strip()
        if not lyrics_api.user_file(title, artist).exists():
            self.status.configure(text="You have no saved version of this song.")
            return
        if messagebox.askyesno("Delete", f"Delete your saved lyrics for “{title}”?", parent=self.win):
            lyrics_api.delete_user(title, artist)
            self.status.configure(text="Deleted. The overlay will use online lyrics again.")
            self.on_saved()
