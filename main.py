"""Autosynced Lyrics: synced lyrics for Spotify, Apple Music, NetEase or QQ Music, floating over the desktop.

Runs in the system tray. The lyrics bar appears while one of those music apps is open.
Launching it again (e.g. from the desktop shortcut) opens the app window (app_window.py),
a separate process that talks to this one through app_server.py.
"""
import sys

if __name__ == "__main__" and "--whisper-worker" in sys.argv:   # the speech-model helper
    import whisper_proc                                           # (see whisper_proc.py): start
    sys.exit(whisper_proc.serve())                                # it before loading the app

import ctypes
import queue
import socket
import threading
import time
import tkinter as tk

import requests

import ai_translate
import app_server
import app_window
import config
import installer
import palette
import paths
import lyrics as lyrics_api
import render
import song_offsets
import sources
import theme
import usage_window
import word_learner
import tray
import updater
from lyrics_editor import LyricsEditor
from overlay import AI_MARK, Overlay, View
from settings_window import SettingsWindow
from media_watcher import MediaWatcher

_winmm = ctypes.WinDLL("winmm")

NETWORK_ERROR = "Network error, unable to fetch lyrics"

IPC_PORT = 47811  # localhost only; used to make the app single-instance


def claim_single_instance(events) -> bool:
    """Return True if we're the first instance. Otherwise ask the running one to open settings."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", IPC_PORT))
    except OSError:
        try:
            with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2) as c:
                c.sendall(b"window")
        except OSError:
            pass
        return False
    server.listen()

    def serve():
        while True:
            conn, _ = server.accept()
            with conn:
                msg = conn.recv(64).strip()
                if msg in (b"settings", b"window"):
                    events.put("window")
                elif msg == b"quit":                 # the installer replacing this copy
                    events.put("quit")
    threading.Thread(target=serve, daemon=True).start()
    return True


class App:
    def __init__(self, events: queue.Queue):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI
        except Exception:
            pass

        self.events = events
        self.settings = config.load()
        if self.settings["learn_word_timing"] is None:
            # First run with this option: on if a speech model is already downloaded
            # (you've used auto-sync), off otherwise so nobody gets a surprise download.
            self.settings["learn_word_timing"] = word_learner.have_model()
        if self.settings["fix_timing_by_listening"] is None:
            self.settings["fix_timing_by_listening"] = word_learner.have_model()
        self.root = tk.Tk()
        self.root.withdraw()  # hidden owner; the overlay and settings are Toplevels
        self.root.iconbitmap(default=str(tray.ensure_ico()))
        theme.apply(self.root)            # Windows 11 look for Settings and the editor

        self.overlay = Overlay(self.root, self.settings, on_moved=self._moved, on_menu=self._menu,
                               on_control=self._control)
        self.settings_win: SettingsWindow | None = None
        self.editor: LyricsEditor | None = None
        self.hidden_by_user = False
        self._hires_timer = False

        self.track_key = None
        self.lyrics: lyrics_api.Lyrics | None = None
        self.status = ""

        self.watcher = MediaWatcher(player=self.settings["player"])
        self.watcher.start()
        self.learner = word_learner.WordLearner(self.watcher)
        self.learner.start()
        self._learn_key = None
        self.icon = tray.start(events)
        self.server = app_server.AppServer(self)
        self.window_proc = None
        self.update_info = None          # a newer version on GitHub (updater.check())
        self.update_state = ""           # "", "downloading 40%", "installing", or an error
        self._update_told = None
        self._tick()
        config.refresh_startup()
        if "--tray" not in sys.argv:            # opened by you (shortcut, Start menu): show the window
            self.root.after(600, self.open_window)
        self.root.after(15_000, self._auto_update_check)

    # ---- updates (from GitHub releases) -------------------------------------
    UPDATE_EVERY_MS = 6 * 3600 * 1000

    def _auto_update_check(self):
        if self.settings.get("check_updates", True) and paths.FROZEN:
            self.check_for_update()
        self.root.after(self.UPDATE_EVERY_MS, self._auto_update_check)

    def check_for_update(self, done=None):
        """Look on GitHub in the background; done(info, error) runs on the Tk thread."""
        def work():
            try:
                info, err = updater.check(), ""
            except Exception as exc:              # offline, GitHub blocked...
                info, err = None, f"Couldn't check for updates ({type(exc).__name__})."
            self.events.put(("call", lambda: self._update_checked(info, err, done)))
        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, info, err, done):
        if not err:
            self.update_info = info
        if info and self._update_told != info["version"]:
            self._update_told = info["version"]
            try:
                self.icon.notify(f"Version {info['version']} is available. Open the app to update.",
                                 "Autosynced Lyrics update")
            except Exception:
                pass
        if done:
            done(info, err)

    def install_update(self):
        """Download the small update, install it and restart (runs in the background)."""
        info = self.update_info
        if not info or self.update_state.startswith(("downloading", "installing")):
            return
        self.update_state = "downloading 0%"

        def progress(pct):
            self.update_state = f"downloading {pct}%"

        def work():
            try:
                updater.download_and_apply(info, progress)
            except Exception as exc:
                msg = str(exc) if isinstance(exc, ValueError) else f"The update didn't work ({type(exc).__name__})."
                self.update_state = "error: " + msg
                return
            self.update_state = "installing"
            self.events.put(("call", self._restart_after_update))
        threading.Thread(target=work, daemon=True).start()

    def _restart_after_update(self):
        updater.restart()
        self.root.after(800, self.quit)

    # ---- actions ----------------------------------------------------------
    def settings_changed(self):
        config.save(self.settings)
        self.watcher.player = self.settings["player"]
        if self._wants_extras() and self.lyrics and not self.lyrics.extras_loaded:
            self.track_key = None            # reload to fetch translations / word timing
        elif self._ai_wanted() and self.lyrics and not self.lyrics.ai_checked:
            self.track_key = None            # reload to fill missing lines with AI translation
        self.overlay.apply()

    def _wants_extras(self) -> bool:
        return self.settings["show_translation"] or self.settings["bouncing_note"]

    def _control(self, name):
        """A hover control on the lyrics bar was clicked."""
        if name == "prev":
            self.watcher.previous_track()
        elif name == "play":
            self.watcher.toggle_play()
        elif name == "next":
            self.watcher.next_track()
        elif name in ("earlier", "later"):
            track = self.watcher.track
            if track is not None:
                v = song_offsets.nudge(track.title, track.artist,
                                       song_offsets.STEP * (1 if name == "earlier" else -1))
                self.overlay.flash("This song's timing: back to normal" if not v else
                                   f"This song: lyrics {abs(v):.2f} s {'earlier' if v > 0 else 'later'}")
        elif name == "settings":
            self.open_settings_page()
        elif name == "lock":
            self.settings["lock_position"] = not self.settings["lock_position"]
            self.settings_changed()

    def _update_colors(self, track):
        """Album-cover colours for the current song (kept until the new cover has arrived)."""
        if not self.settings["album_colors"]:
            self.overlay.set_colors(None)
            return
        cover = self.watcher.cover
        if cover is None or cover[0] != track.key:
            return
        light = self.settings["bar_follows_style"] and self.settings["ui_style"] == "day"
        if self._colors_for != (cover[0], light):
            self._colors_for = (cover[0], light)
            self.overlay.set_colors(palette.from_cover(cover[1], light) if cover[1] else None)

    _colors_for = None

    def _moved(self):
        config.save(self.settings)

    def open_settings_page(self):
        """Settings in the app window (the classic settings window if that can't run)."""
        self._goto = {"page": "settings"}
        self.open_window(fallback=self.open_settings)

    def open_settings(self):
        if self.settings_win and self.settings_win.exists():
            self.settings_win.focus()
        else:
            self.settings_win = SettingsWindow(self.root, self.settings, self.settings_changed)

    def open_editor(self, title: str | None = None, artist: str | None = None):
        """The Editor page of the app window (the classic editor window if that can't run)."""
        self._goto = {"page": "editor", "title": title, "artist": artist}
        self.open_window(fallback=lambda: self._open_classic_editor(title, artist))

    def _open_classic_editor(self, title=None, artist=None):
        self._goto = None
        if self.editor and self.editor.exists():
            self.editor.focus()
        else:
            self.editor = LyricsEditor(self.root, self.watcher, self.lyrics_saved)
        if title is not None:
            self.editor.open_song(title, artist or "")

    _goto = None

    def take_goto(self):
        """A page the app window should switch to (asked for once, then forgotten)."""
        g, self._goto = self._goto, None
        return g

    def open_window(self, fallback=None):
        """Open the main app window (or bring it to the front). Falls back to the classic
        settings window (or `fallback`) if the app window can't run on this PC."""
        fallback = fallback or self.open_settings
        if self.window_proc is not None and self.window_proc.poll() is None:
            app_window.bring_to_front()
            return
        try:
            self.window_proc = app_window.launch(self.server.url, self.settings["ui_style"])
        except OSError:
            self.window_proc = None
            fallback()
            return

        def check():   # window process gave up straight away (no WebView2 etc.)
            if self.window_proc is not None and self.window_proc.poll() not in (None, 0):
                self.window_proc = None
                fallback()
        self.root.after(4000, check)

    def lyrics_saved(self):
        self.track_key = None  # forces the current song's lyrics to reload

    def _menu(self, x, y):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Open Autosynced Lyrics", command=self.open_window)
        m.add_command(label="Edit / sync lyrics…", command=self.open_editor)
        m.add_command(label="Settings…", command=self.open_settings_page)
        if self.settings["ai_translate"] or ai_translate.usage_log():
            m.add_command(label="AI translation usage & cost…",
                          command=lambda: usage_window.show(self.root))
        m.add_command(label="Hide lyrics", command=lambda: self.events.put("toggle"))
        m.add_separator()
        m.add_command(label="Quit", command=self.quit)
        m.tk_popup(x, y)

    def quit(self):
        self.icon.stop()
        if self.window_proc is not None and self.window_proc.poll() is None:
            self.window_proc.terminate()
        self.root.destroy()

    # ---- main loop --------------------------------------------------------
    def _handle_events(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(ev, tuple) and ev[0] == "call":
                try:
                    ev[1]()                     # a change requested by the app window
                except Exception:
                    pass
            elif ev == "window":
                self.open_window()
            elif ev == "settings":
                self.open_settings_page()
            elif ev == "editor":
                self.open_editor()
            elif ev == "unlock":
                self.settings["lock_position"] = not self.settings["lock_position"]
                self.settings_changed()
            elif ev == "toggle":
                self.hidden_by_user = not self.hidden_by_user
            elif ev == "quit":
                self.quit()
                return

    def _load_lyrics(self, track):
        use_cache = self.settings["cache_lyrics"]
        extras = None   # translations / word timing, looked up while the lyrics are
        try:
            found, result = lyrics_api.fetch_local(track.title, track.artist, track.duration, use_cache)
            if not found:
                if self._wants_extras():
                    extras = lyrics_api.fetch_extras(track.title, track.artist, track.duration)
                result = lyrics_api.fetch_online(track.title, track.artist, track.album,
                                                 track.duration, use_cache)
            status = "" if result else "No lyrics found"
        except requests.RequestException:
            result, status = None, NETWORK_ERROR
        except Exception as exc:
            result, status = None, f"Lyrics error: {exc}"
        if self.track_key != track.key:   # the song changed meanwhile
            return
        # Show the lyrics now; translations / word timing follow if they aren't in yet.
        self.lyrics, self.status = result, status
        self._note_line = None
        if result is None or not result.synced or not self._wants_extras():
            return
        if not result.extras_loaded:
            self._load_extras(track, result, extras)
        self._ai_fill(track, result)

    def _load_extras(self, track, result, extras):
        extra = lyrics_api.Lyrics(result.lines, True)   # filled off to the side, swapped in at once
        try:   # Chinese translations + per-word timing; fine to do without
            extras = extras or lyrics_api.fetch_extras(track.title, track.artist, track.duration)
            lyrics_api.apply_extras(extra, extras.result())
        except Exception:
            pass
        result.translations, result.char_times = extra.translations, extra.char_times
        result.extras_loaded = True
        if self.settings["cache_lyrics"]:
            lyrics_api.remember_extras(track.title, track.artist, track.duration, result)
        if self.track_key == track.key:
            self._note_line = None        # the note picks up the real word timing

    def _ai_wanted(self) -> bool:
        return bool(self.settings["show_translation"] and self.settings["ai_translate"]
                    and ai_translate.has_key())

    def _ai_fill(self, track, lyr):
        """Claude translations for the lines no site translated (runs on the loading thread)."""
        if not self._ai_wanted() or lyr.is_chinese or lyr.ai_checked:
            return
        lyr.ai_checked = True
        texts = [t for _, t in lyr.lines]
        want = [i for i, t in enumerate(texts)
                if ai_translate.worth_translating(t) and not lyr.translations[i]]
        if not want:
            return
        try:
            got = ai_translate.translate(track.title, track.artist, texts, lyr.translations, want)
        except Exception as exc:
            self.ai_status = f"AI translation failed: {str(exc)[:120]}"
            self._pending_flash = "AI translation failed (see Settings)"
            return
        self.ai_status = f"AI translated {len(got)} line(s) of the last song"
        for i, zh in got.items():
            if not lyr.translations[i]:
                lyr.translations[i] = zh
                lyr.ai_lines.add(i)

    ai_status = ""
    _pending_flash = None

    def _tick(self):
        started = time.perf_counter()
        self._handle_events()
        if not self.root.winfo_exists():
            return
        track = self.watcher.track
        player_open = track is not None
        self.overlay.set_visible(not self.hidden_by_user
                                 and (player_open or not self.settings["hide_when_closed"]))
        if not player_open:
            self.track_key, self.lyrics = None, None
            self.overlay.set_view(View(cur=self._waiting_text()))
            self.overlay.set_progress(None, 0)
        else:
            self.overlay.set_progress(track.current_position, track.duration, track.playing)
            if not self.settings["album_colors"]:
                self._colors_for = None
            self._update_colors(track)
            if track.key != self.track_key:
                self.track_key, self.lyrics, self.status = track.key, None, "Loading lyrics…"
                threading.Thread(target=self._load_lyrics, args=(track,), daemon=True).start()
            self._render(track)
        self._learn(track)
        if self._pending_flash:
            self.overlay.flash(self._pending_flash, 4.0)
            self._pending_flash = None
        self.overlay.tick()

        # While animating: ask Windows for 1 ms timer precision (default ~15.6 ms makes
        # 16 ms waits jitter up to 31 ms) and subtract drawing time from the wait.
        animating = self.overlay.busy()
        if animating != self._hires_timer:
            try:
                (_winmm.timeBeginPeriod if animating else _winmm.timeEndPeriod)(1)
            except Exception:
                pass
            self._hires_timer = animating
        spent_ms = (time.perf_counter() - started) * 1000
        self.root.after(max(1, round(16 - spent_ms)) if animating else 16, self._tick)

    RETIME_ENOUGH = 12      # lines measured before the timing fix is considered settled

    def _set_learn_target(self, track, lyr, words_on, fix_on):
        wanted = [i for i, (_, text) in enumerate(lyr.lines)
                  if words_on and lyrics_api._squash(text) and not lyrics_api.has_word_timing(lyr, i)]
        measure = [i for i, (_, text) in enumerate(lyr.lines)
                   if fix_on and i not in lyr.heard_starts
                   and len(word_learner.word_slots(text)) >= word_learner.MEASURE_MIN_WORDS]
        self.learner.set_target(track.key, lyr.lines, wanted, measure)

    def _learn(self, track):
        """Learn word timing by listening, for lines no site had it for (see word_learner)."""
        lyr = self.lyrics
        ready = track is not None and lyr is not None and lyr.synced and lyr.extras_loaded
        words_on = bool(ready and self.settings["learn_word_timing"] and self.settings["bouncing_note"])
        # Only lyrics found online get re-timed: ones you synced yourself are left as they are.
        if lyr is not None and len(lyr.heard_starts) >= self.RETIME_ENOUGH:
            lyr.retime_done = True               # enough to know the timing: stop listening for it
        fix_on = bool(ready and self.settings["fix_timing_by_listening"] and lyr.online
                      and not lyr.retime_done)
        self.learner.enabled = words_on or fix_on
        if not (words_on or fix_on):
            self._learn_key = None
            return
        key = (track.key, id(lyr), words_on, fix_on)
        if key != self._learn_key:               # new song / lyrics: tell it what's missing
            self._learn_key = key
            self._set_learn_target(track, lyr, words_on, fix_on)
        changed = moved = False
        while True:
            try:
                song, found, starts = self.learner.results.get_nowait()
            except queue.Empty:
                break
            if song != track.key or self.lyrics is not lyr:
                continue
            for i, chars in found.items():
                if i < len(lyr.char_times) and not lyr.char_times[i]:
                    lyr.char_times[i] = chars
                    lyr.heard_lines.add(i)
                    changed = True
            if starts and fix_on:
                lyr.heard_starts.update(starts)
                if lyrics_api.apply_retime(lyr):
                    changed = moved = True
        if moved:
            # Your own earlier/later fix for this song was making up for the same delay.
            if song_offsets.get(track.title, track.artist):
                song_offsets.nudge(track.title, track.artist, -song_offsets.get(track.title, track.artist))
            self._set_learn_target(track, lyr, words_on, fix_on)   # it matches against the new times
            self._note_line = None
        if changed:
            self._note_line = None               # the note switches to the learned timing
            if self.settings["cache_lyrics"]:
                lyrics_api.remember_extras(track.title, track.artist, track.duration, lyr)

    def _waiting_text(self) -> str:
        from media_watcher import PLAYERS
        name = PLAYERS.get(self.settings["player"], ("your music app",))[0]
        return f"Waiting for {name}…"

    def _render(self, track):
        header = f"{track.title} — {track.artist}"
        song = track.key
        lyr = self.lyrics
        if lyr is None:
            self.overlay.set_view(View(header, "", self.status or "♪", "", None, song))
        elif not lyr.synced:
            self.overlay.set_view(View(header, "", "No synced lyrics for this song yet",
                                       "Right-click → Edit / sync lyrics to add them", None, song))
        else:
            fix = self.settings["offset"] + song_offsets.get(track.title, track.artist)
            pos = lambda: track.current_position() + fix
            i = lyr.index_at(pos())
            line = lambda j: (lyr.lines[j][1] or "♪") if 0 <= j < len(lyr.lines) else ""
            tr_on = self.settings["show_translation"] and lyr.has_translation and not lyr.is_chinese
            tr = lambda j: ((AI_MARK if j in lyr.ai_lines else "") + (lyr.translations[j] or "")
                            if tr_on and 0 <= j < len(lyr.lines) and lyr.translations[j] else "")
            if i < 0:
                self.overlay.set_view(View(header, "", "♪", line(0), -1, song, "", "", tr(0), tr_on))
            else:
                self.overlay.set_view(View(header, line(i - 1), line(i), line(i + 1), i, song,
                                           tr(i - 1), tr(i), tr(i + 1), tr_on))
            self._update_note(lyr, i, song, pos)

    _note_line = None

    def _update_note(self, lyr, i, song, pos):
        """Word start times for the bouncing note, worked out once per line."""
        if not self.settings["bouncing_note"]:
            self.overlay.set_note(None, pos)
            return
        if self._note_line != (song, i):
            self._note_line = (song, i)
            text = lyr.lines[i][1] if 0 <= i < len(lyr.lines) else ""
            words = render.tokens(text) if text.strip() and text.strip() != "♪" else []
            starts, end = lyrics_api.word_times(lyr, i, words) if words else ([], 0.0)
            real = lyrics_api.has_word_timing(lyr, i)
            self._note = (song, i, starts, end, real) if starts else None
        self.overlay.set_note(self._note, pos)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import selfcheck
        sys.exit(selfcheck.run())
    if "--window" in sys.argv:                     # the app window's own process
        sys.exit(app_window.main(sys.argv))
    if "--uninstall" in sys.argv:                  # from Settings -> Apps -> Installed apps
        sys.exit(installer.run_uninstall())
    if "--tray" not in sys.argv and installer.should_offer() and installer.offer_install():
        sys.exit(0)                                # the installed copy has been started
    events = queue.Queue()
    if not claim_single_instance(events):
        sys.exit(0)
    paths.prepare()  # create the AppData folder (and move old data there once)
    updater.cleanup()  # files the last update left behind
    App(events).run()
