"""Small window for adjusting the overlay. Every change applies live and is saved immediately."""
import tkinter as tk
from tkinter import colorchooser, ttk

import cache
import config
import theme
import fonts
from media_watcher import PLAYER_CHOICES
from overlay import ANIMATIONS


class SettingsWindow:
    def __init__(self, master, settings: dict, on_change):
        self.s = settings
        self.on_change = on_change
        self.refreshers = []   # callbacks that push current settings back into widgets

        self.win = tk.Toplevel(master)
        self.win.title("Lyrics overlay settings")
        theme.title_bar(self.win)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        outer = ttk.Frame(self.win, padding=10)
        outer.pack(fill="both", expand=True)
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)

        p = self._tab(tabs, "Look")
        self._scale(p, "Overall opacity", "opacity", 0.2, 1.0, 0.05)
        self._scale(p, "Background opacity (0 = text only)", "bg_opacity", 0.0, 1.0, 0.05)
        self._color(p, "Background color", "bg_color")
        self._check(p, "Soft background with a shadow", "soft_background")
        self._check(p, "Colours from the album cover\n(current line and background)", "album_colors")
        self._combo(p, "Font", "font_family", fonts.family_names())
        self._section(p, "Outline")
        self._check(p, "Outline the lyrics", "outline_enabled")
        self._color(p, "Outline color", "outline_color")
        self._spin(p, "Outline thickness", "outline_width", 1, 8, 1)
        self._section(p, "Size & position")
        self._scale(p, "Width (% of screen)", "width_pct", 20, 100, 5)

        p = self._tab(tabs, "Text")
        self._check(p, "Song progress line along the bottom", "show_progress")
        self._section(p, "Song title & artist (top)")
        self._check(p, "Show title and artist", "show_header")
        self._spin(p, "Size", "header_size", 6, 48, 1)
        self._color(p, "Color", "header_color")
        self._section(p, "Current line")
        self._spin(p, "Size", "current_size", 8, 96, 1)
        self._check(p, "Bold", "bold_current")
        self._color(p, "Color", "current_color")
        self._section(p, "Previous / next lines")
        self._check(p, "Show previous line", "show_prev")
        self._check(p, "Show next line", "show_next")
        self._spin(p, "Size", "other_size", 6, 72, 1)
        self._color(p, "Color", "other_color")
        self._section(p, "Chinese translation")
        self._check(p, "Show the Chinese translation under each line", "show_translation")
        self._spin(p, "Size (% of the line)", "translation_size_pct", 30, 100, 5)
        self._check(p, "Fill lines with no translation using AI (Claude, your API key)\n"
                       "Haiku for a few missing lines, Sonnet for whole songs; shown dimmer",
                    "ai_translate")
        self._api_key_row(p)

        p = self._tab(tabs, "Animation")
        self._combo(p, "Line change effect", "animation", list(ANIMATIONS), mapping=ANIMATIONS)
        self._scale(p, "Duration (ms)", "animation_ms", 100, 1200, 50)
        self._section(p, "Bouncing note")
        self._check(p, "A note hops along the words as they're sung", "bouncing_note")
        self._spin(p, "Note timing (s, + = earlier)", "note_offset", -0.5, 0.5, 0.02)
        self._check(p, "Learn word timing by listening when no lyrics site has it\n"
                       "(uses some CPU while a new song plays; needs a speech model)",
                    "learn_word_timing")
        self._section(p, "Timing")
        self._check(p, "Fix lyrics by listening: the right version of the song\n"
                       "(feat., remix, clean…) and the right timing", "fix_timing_by_listening")
        self._spin(p, "Lyrics offset (s, + = earlier)", "offset", -10, 10, 0.1)

        p = self._tab(tabs, "Behavior")
        self._combo(p, "Music app", "player", list(PLAYER_CHOICES), mapping=PLAYER_CHOICES)
        self._check(p, "Hide lyrics when no music app is open", "hide_when_closed")
        self._check(p, "Show controls when the mouse is over the lyrics\n"
                       "(previous, play/pause, next, settings, lock)", "hover_controls")
        self._check(p, "Lock in place & let clicks pass through\n"
                       "(press Ctrl over the lyrics for the controls; unlock here or from the tray)",
                    "lock_position")
        self.startup_var = tk.BooleanVar(value=config.startup_enabled())
        ttk.Checkbutton(p, text="Start automatically when I log in to Windows",
                        variable=self.startup_var,
                        command=lambda: config.set_startup(self.startup_var.get())
                        ).grid(row=self._row(p), column=0, columnspan=2, sticky="w", padx=4, pady=2)
        self._section(p, "Lyrics cache")
        self._check(p, "Remember lyrics found online so replayed songs load instantly\n"
                       f"(at most {cache.MAX_BYTES // 1024 // 1024} MB; oldest dropped first)",
                    "cache_lyrics")
        row = ttk.Frame(p)
        row.grid(row=self._row(p), column=0, columnspan=2, sticky="w", padx=4, pady=2)
        usage = ttk.Label(row, foreground=theme.MUTED)

        def show_usage():
            n, kb = cache.count(), cache.size() / 1024
            usage.configure(text=f"{n} song{'s' * (n != 1)}, {kb:,.0f} KB" if n else "Empty")

        def clear():
            cache.clear()
            show_usage()
        ttk.Button(row, text="Clear cache", command=clear).pack(side="left")
        usage.pack(side="left", padx=8)
        show_usage()

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Reset to defaults", command=self._reset).pack(side="left")
        ttk.Label(btns, text="Changes save automatically", foreground=theme.MUTED).pack(side="left", padx=10)
        ttk.Button(btns, text="Close", command=self.win.destroy).pack(side="right")

    def _api_key_row(self, p):
        """Paste-in box for the user's own Claude API key (stored encrypted for this
        Windows account; never shown again once saved)."""
        import ai_translate
        r = self._label(p, "Claude API key")
        row = ttk.Frame(p)
        row.grid(row=r, column=1, sticky="w")
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var, show="•", width=22).pack(side="left")
        status = ttk.Label(p, foreground=theme.MUTED)
        status.grid(row=self._row(p), column=0, columnspan=2, sticky="w", padx=4)

        def show():
            status.configure(text="Key saved (encrypted for your Windows account)."
                             if ai_translate.has_key() else
                             "No key yet: create one at console.anthropic.com (pay-as-you-go).")

        def save():
            ai_translate.set_key(var.get())
            var.set("")
            show()
            self.on_change()

        def forget():
            ai_translate.set_key(None)
            show()
        ttk.Button(row, text="Save", command=save).pack(side="left", padx=4)
        ttk.Button(row, text="Remove", command=forget).pack(side="left")
        import usage_window
        ttk.Button(row, text="Usage & cost…",
                   command=lambda: usage_window.show(self.win)).pack(side="left", padx=(8, 0))
        show()

    # ---- widget builders --------------------------------------------------
    def _tab(self, tabs, title):
        f = ttk.Frame(tabs, padding=12)
        f.rows = 0
        tabs.add(f, text=title)
        return f

    @staticmethod
    def _row(parent):
        parent.rows += 1
        return parent.rows

    def _section(self, p, title):
        ttk.Label(p, text=title, font=("Segoe UI", 10, "bold")).grid(
            row=self._row(p), column=0, columnspan=2, sticky="w", pady=(10, 2))

    def _label(self, p, text):
        r = self._row(p)
        ttk.Label(p, text=text).grid(row=r, column=0, sticky="w", padx=(4, 16), pady=3)
        return r

    def _set(self, key, value):
        if self.s.get(key) != value:
            self.s[key] = value
            self.on_change()

    def _check(self, p, text, key):
        var = tk.BooleanVar(value=self.s[key])
        ttk.Checkbutton(p, text=text, variable=var, command=lambda: self._set(key, var.get())
                        ).grid(row=self._row(p), column=0, columnspan=2, sticky="w", padx=4, pady=2)
        self.refreshers.append(lambda: var.set(self.s[key]))

    def _scale(self, p, text, key, lo, hi, step):
        r = self._label(p, text)
        var = tk.DoubleVar(value=self.s[key])
        value_lbl = ttk.Label(p, width=5)
        is_int = isinstance(step, int)

        def show():
            v = self.s[key]
            value_lbl.configure(text=f"{v}" if is_int else f"{round(v * 100)}%")

        def changed(_=None):
            v = round(round(var.get() / step) * step, 2)
            self._set(key, int(v) if is_int else v)
            show()
        ttk.Scale(p, from_=lo, to=hi, variable=var, command=changed, length=200
                  ).grid(row=r, column=1, sticky="we")
        value_lbl.grid(row=r, column=2, sticky="w", padx=(6, 0))
        show()
        self.refreshers.append(lambda: (var.set(self.s[key]), show()))

    def _spin(self, p, text, key, lo, hi, step):
        r = self._label(p, text)
        var = tk.StringVar(value=str(self.s[key]))
        is_int = isinstance(step, int)

        def changed(*_):
            try:
                v = int(float(var.get())) if is_int else round(float(var.get()), 2)
            except ValueError:
                return
            self._set(key, max(lo, min(hi, v)))
        ttk.Spinbox(p, from_=lo, to=hi, increment=step, textvariable=var, width=8,
                    command=changed).grid(row=r, column=1, sticky="w")
        var.trace_add("write", changed)
        self.refreshers.append(lambda: var.set(str(self.s[key])))

    def _combo(self, p, text, key, values, mapping=None):
        r = self._label(p, text)
        to_label = {v: k for k, v in mapping.items()} if mapping else {}
        var = tk.StringVar(value=to_label.get(self.s[key], self.s[key]))
        box = ttk.Combobox(p, textvariable=var, values=values, state="readonly", width=26)
        box.grid(row=r, column=1, columnspan=2, sticky="w")
        box.bind("<<ComboboxSelected>>",
                 lambda e: self._set(key, mapping[var.get()] if mapping else var.get()))
        self.refreshers.append(lambda: var.set(to_label.get(self.s[key], self.s[key])))

    def _color(self, p, text, key):
        r = self._label(p, text)
        btn = tk.Button(p, width=6, relief="groove")

        def show():
            btn.configure(bg=self.s[key], activebackground=self.s[key])

        def pick():
            _, hex_color = colorchooser.askcolor(self.s[key], parent=self.win, title=text)
            if hex_color:
                self._set(key, hex_color)
                show()
        btn.configure(command=pick)
        btn.grid(row=r, column=1, sticky="w")
        show()
        self.refreshers.append(show)

    def _reset(self):
        x, y = self.s["x"], self.s["y"]
        self.s.update(config.DEFAULTS)
        if self.s["learn_word_timing"] is None:
            import word_learner
            self.s["learn_word_timing"] = word_learner.have_model()
        if self.s["fix_timing_by_listening"] is None:
            import word_learner
            self.s["fix_timing_by_listening"] = word_learner.have_model()
        self.s["x"], self.s["y"] = x, y   # keep where the user put it
        for refresh in self.refreshers:
            refresh()
        self.on_change()

    # ---- window -----------------------------------------------------------
    def exists(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def focus(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
