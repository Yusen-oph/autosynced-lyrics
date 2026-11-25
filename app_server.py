"""Local web server that the main app window (ui/ + app_window.py) talks to.

- Listens on 127.0.0.1 only, on a free port chosen at start.
- Serves the window's page (ui/) and a small JSON API with the app's state.
- Every API call must carry the random token given to the window process, so other
  programs or web pages on this computer can't use it.
- Handlers only read app state. Anything that changes it is queued and run on the app's
  Tk thread (App.events), like the tray menu does.
"""
import json
import os
import mimetypes
import re
import secrets
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ai_translate
import cloud_sync
import cache
import config
import fonts
import installer
import lyrics as lyrics_api
import paths
import updater
import song_offsets
import sources
from media_watcher import PLAYER_CHOICES
from overlay import ANIMATIONS

UI_DIR = paths.CODE_DIR / "ui"
ALLOWED = {   # text settings that only take these values
    "ui_style": ("night", "day"),
    "cover_source": ("album", "gradient", "none"),
    "cover_shape": ("square", "rounded", "circle", "vinyl", "cd", "polaroid"),
    "cover_motion": ("still", "spin", "float", "sway", "pulse", "tilt"),
    "ai_provider": tuple(ai_translate.PROVIDERS),
}


class AppServer:
    def __init__(self, app):
        self.app = app
        self.token = secrets.token_urlsafe(24)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):            # quiet
                pass

            def do_GET(self):
                server._handle(self, "GET")

            def do_POST(self):
                server._handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    # ---- plumbing --------------------------------------------------------------
    def _send(self, h, status=200, body=b"", ctype="application/json; charset=utf-8", cache_s=0):
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", f"max-age={cache_s}" if cache_s else "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.end_headers()
        h.wfile.write(body)

    def _json(self, h, data, status=200):
        self._send(h, status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _run_on_app(self, fn):
        """Run fn on the app's Tk thread (the only thread allowed to change app state)."""
        self.app.events.put(("call", fn))

    def _handle(self, h, method):
        url = urlparse(h.path)
        path = url.path
        try:
            if not path.startswith("/api/"):
                return self._static(h, path)
            if not secrets.compare_digest(h.headers.get("X-Token", ""), self.token):
                return self._json(h, {"error": "forbidden"}, 403)
            body = {}
            if method == "POST" and path == "/api/update":     # a zip file, not JSON
                n = int(h.headers.get("Content-Length") or 0)
                if n > updater.MAX_SIZE:
                    return self._json(h, {"error": "That file is too big to be an update."}, 400)
                return self._json(h, self._apply_update(h.rfile.read(n)))
            if method == "POST":
                n = int(h.headers.get("Content-Length") or 0)
                body = json.loads(h.rfile.read(min(n, 1_000_000)) or b"{}") if n else {}
            route = getattr(self, f"_{method.lower()}_{path[5:].replace('/', '_').replace('-', '_')}", None)
            if route is None:
                return self._json(h, {"error": "not found"}, 404)
            result = route(body if method == "POST" else parse_qs(url.query))
            if isinstance(result, tuple):             # (bytes, content type)
                return self._send(h, 200, result[0], result[1], cache_s=3600)
            self._json(h, result if result is not None else {"ok": True})
        except Exception as exc:
            self._json(h, {"error": str(exc)[:200]}, 500)

    def _static(self, h, path):
        rel = path.lstrip("/") or "index.html"
        f = (UI_DIR / rel).resolve()
        if UI_DIR.resolve() not in f.parents or not f.is_file():
            return self._send(h, 404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(h, 200, f.read_bytes(), ctype)

    # ---- now playing -------------------------------------------------------------
    def _song_fix(self, track) -> float:
        return self.app.settings["offset"] + song_offsets.get(track.title, track.artist)

    def _get_now(self, q):
        app, t = self.app, self.app.watcher.track
        goto = app.take_goto()
        if t is None:
            return {"open": False, "waiting": app._waiting_text(), "style": app.settings["ui_style"],
                    "goto": goto, "update": self._update_status(),
                    "cover": self._cover_style()}
        lyr = app.lyrics if app.track_key == t.key else None
        cover = app.watcher.cover
        return {
            "open": True, "title": t.title, "artist": t.artist, "album": t.album,
            "duration": t.duration, "position": t.current_position(), "playing": t.playing,
            "player": t.player, "fix": self._song_fix(t), "song_fix": song_offsets.get(t.title, t.artist),
            "status": app.status, "lyrics_rev": self._lyrics_rev(lyr),
            "cover_id": f"{abs(hash(cover[1])) if cover and cover[0] == t.key and cover[1] else ''}",
            "style": app.settings["ui_style"], "show_translation": app.settings["show_translation"],
            "bar_hidden": app.hidden_by_user, "can_seek": app.watcher.can_seek,
            "cover": self._cover_style(), "goto": goto, "update": self._update_status(),
        }

    def _update_status(self):
        info = self.app.update_info
        if not info:
            return None
        return {"version": info["version"], "small_ok": info["small_ok"], "notes": info["notes"],
                "state": self.app.update_state}

    def _post_update_check(self, b):
        """Check GitHub now (Settings -> About). Waits for the answer."""
        done = threading.Event()
        out = {}

        def finished(info, err):
            out.update(info=info, err=err)
            done.set()
        self._run_on_app(lambda: self.app.check_for_update(finished))
        if not done.wait(20):
            return {"error": "Checking took too long."}
        if out["err"]:
            return {"error": out["err"]}
        info = out["info"]
        return {"latest": not info, "update": self._update_status()}

    def _post_update_install(self, b):
        if not self.app.update_info:
            return {"error": "No update to install."}
        if not self.app.update_info["small_ok"]:
            page = self.app.update_info["page"]
            if not page.startswith(f"https://github.com/{updater.REPO}/"):
                page = updater.RELEASES_PAGE
            os.startfile(page)                    # the full download, in the browser
            return {"opened": True}
        self._run_on_app(self.app.install_update)
        return {"ok": True}

    def _cover_style(self) -> dict:
        s = self.app.settings
        return {"source": s["cover_source"], "shape": s["cover_shape"], "motion": s["cover_motion"], "speed": s["cover_speed"],
                "arm": s["cover_arm"], "glow": s["cover_glow"], "reflect": s["cover_reflect"]}

    @staticmethod
    def _lyrics_rev(lyr) -> str:
        if lyr is None:
            return ""
        return (f"{id(lyr)}:{sum(1 for x in lyr.translations if x)}:{len(lyr.ai_lines)}:"
                f"{sum(1 for c in lyr.char_times if c)}")

    def _get_lyrics(self, q):
        app, t = self.app, self.app.watcher.track
        lyr = app.lyrics if t is not None and app.track_key == t.key else None
        if lyr is None:
            return {"rev": "", "synced": False, "lines": [], "status": app.status if t else ""}
        tr_on = app.settings["show_translation"]
        shown = lyr.shown_times()            # music-break lines wait for the last word to end
        return {
            "rev": self._lyrics_rev(lyr), "synced": lyr.synced, "status": app.status,
            "lines": [{"t": round(shown[i], 3), "text": text,
                       "tr": (lyr.translations[i] or "") if tr_on else "",
                       "ai": i in lyr.ai_lines}
                      for i, (tm, text) in enumerate(lyr.lines)],
        }

    def _get_cover(self, q):
        cover = self.app.watcher.cover
        if not cover or not cover[1]:
            raise FileNotFoundError("no cover")
        data = cover[1]
        ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return data, ctype

    def _post_control(self, b):
        name, app = b.get("name"), self.app
        w = app.watcher
        if name in ("prev", "play", "next"):
            {"prev": w.previous_track, "play": w.toggle_play, "next": w.next_track}[name]()
        elif name == "seek":
            t = w.track
            if t is not None:          # a lyric line's time, or (raw) a position in the song
                to = float(b.get("to", 0))
                w.seek(max(0.0, to if b.get("raw") else to - self._song_fix(t)))
        elif name in ("earlier", "later"):
            self._run_on_app(lambda: app._control(name))
        elif name == "editor":
            self._run_on_app(app.open_editor)
        elif name == "toggle_bar":
            self._run_on_app(lambda: app.events.put("toggle"))
        else:
            raise ValueError("unknown control")

    # ---- settings ------------------------------------------------------------------
    def _get_settings(self, q):
        app = self.app
        return {
            "settings": {k: app.settings[k] for k in config.DEFAULTS if k not in ("x", "y")},
            "choices": {
                "font_family": fonts.family_names(),
                "animation": [[label, value] for label, value in ANIMATIONS.items()],
                "player": [[label, value] for label, value in PLAYER_CHOICES.items()],
                "ai_provider": [[name, p] for p, (name, *_) in ai_translate.PROVIDERS.items()],
            },
            "startup": config.startup_enabled(),
            "cache": {"count": cache.count(), "kb": round(cache.size() / 1024)},
            "ai_key": ai_translate.has_key(),
            "ai_keys": {p: ai_translate.has_key(p) for p in ai_translate.PROVIDERS if p != "machine"},
            "ai_key_sites": {p: v[3] for p, v in ai_translate.PROVIDERS.items() if v[3]},
            "ai_status": app.ai_status,
            "cloud": {"password": cloud_sync.has_password(), "status": cloud_sync.status},
            "about": {"version": installer.VERSION, "installed": installer.is_installed_copy(),
                      "packaged": paths.FROZEN, "location": str(installer.running_exe().parent),
                      "data": str(paths.DATA_DIR), "releases": updater.RELEASES_PAGE},
        }

    def _post_settings(self, b):
        changes = {}
        for k, v in b.items():
            if k not in config.DEFAULTS or k in ("x", "y"):
                continue
            d = config.DEFAULTS[k]
            if isinstance(d, bool) or d is None and isinstance(v, bool):
                v = bool(v)
            elif isinstance(d, int):
                v = int(round(float(v)))
            elif isinstance(d, float):
                v = float(v)
            elif isinstance(d, str):
                v = str(v)[:200]
                if k.endswith("_color") and not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
                    continue
                if k in ALLOWED and v not in ALLOWED[k]:
                    continue
            changes[k] = v

        def apply():
            self.app.settings.update(changes)
            self.app.settings_changed()
        self._run_on_app(apply)
        return {"ok": True, "applied": list(changes)}

    def _post_reset(self, b):
        def apply():
            s = self.app.settings
            x, y = s["x"], s["y"]
            s.update(config.DEFAULTS)
            s["x"], s["y"] = x, y
            if s["learn_word_timing"] is None:
                import word_learner
                s["learn_word_timing"] = word_learner.have_model()
            if s["fix_timing_by_listening"] is None:
                import word_learner
                s["fix_timing_by_listening"] = word_learner.have_model()
            self.app.settings_changed()
        self._run_on_app(apply)

    def _post_startup(self, b):
        config.set_startup(bool(b.get("on")))
        return {"startup": config.startup_enabled()}

    def _apply_update(self, data: bytes):
        try:
            version = updater.apply(data)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"Couldn't replace the app's files: {exc}"}

        def restart():
            updater.restart()
            self.app.quit()
        threading.Timer(1.0, lambda: self._run_on_app(restart)).start()   # let the reply go out first
        return {"ok": True, "version": version}

    def _post_cache_clear(self, b):
        cache.clear()
        return {"count": 0, "kb": 0}

    def _post_ai_key(self, b):
        provider = str(b.get("provider", "claude"))
        if b.get("remove"):
            ai_translate.set_key(None, provider)
        else:
            key = str(b.get("key", "")).strip()
            if not key:
                raise ValueError("empty key")
            ai_translate.set_key(key, provider)
            self._run_on_app(self.app.settings_changed)
        return {"ai_key": ai_translate.has_key(provider), "provider": provider}

    def _post_cloud_password(self, b):
        cloud_sync.set_password(None if b.get("remove") else str(b.get("password", ""))[:500])
        return {"password": cloud_sync.has_password()}

    def _post_cloud_sync(self, b):
        """Upload now (waits for it, so Settings can show how it went)."""
        return {"status": cloud_sync.upload(dict(self.app.settings), force=True)}

    # ---- my lyrics -----------------------------------------------------------------
    def _get_mylyrics(self, q):
        return {"songs": lyrics_api.list_user(), "folder": str(lyrics_api.USER_DIR)}

    def _user_file(self, name: str) -> Path:
        f = (lyrics_api.USER_DIR / str(name)).resolve()
        if f.parent != lyrics_api.USER_DIR.resolve() or f.suffix.lower() != ".lrc" or not f.exists():
            raise ValueError("no such file")
        return f

    def _post_mylyrics_export(self, b):
        """All your lyrics in one zip in Downloads (for the phone app's Import), shown in Explorer."""
        files = sorted(lyrics_api.USER_DIR.glob("*.lrc")) if lyrics_api.USER_DIR.exists() else []
        if not files:
            return {"count": 0}
        out = Path.home() / "Downloads"
        if not out.is_dir():
            out = Path.home()
        out = out / "AutosyncedLyrics-my-lyrics.zip"
        tmp = out.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f.name)
        os.replace(tmp, out)
        subprocess.Popen(["explorer", "/select,", str(out)])
        return {"count": len(files), "name": out.name, "path": str(out)}

    def _post_mylyrics_delete(self, b):
        f = self._user_file(b.get("file"))
        f.unlink()
        self._run_on_app(self.app.lyrics_saved)
        return self._get_mylyrics(None)

    def _post_mylyrics_edit(self, b):
        info = next((s for s in lyrics_api.list_user() if s["file"] == b.get("file")), None)
        if info is None:
            raise ValueError("no such file")
        self._run_on_app(lambda: self.app.open_editor(info["title"], info["artist"]))

    # ---- editor page -----------------------------------------------------------------
    @property
    def jobs(self):
        if getattr(self, "_jobs", None) is None:
            import editor_jobs
            self._jobs = editor_jobs.EditorJobs(self.app.watcher)
        return self._jobs

    def _song_args(self, d) -> tuple[str, str]:
        get = (lambda k: (d.get(k) or [""])[0]) if isinstance(d.get("title", ""), list) else d.get
        return str(get("title") or "").strip(), str(get("artist") or "").strip()

    def _matching_track(self, title, artist):
        """The playing track if the editor's song is it (so its length can pick the right
        version online). Small differences (case, spaces, "feat." or a remaster tag) still count."""
        t = self.app.watcher.track
        if t is None:
            return None
        same = lambda a, b: (a or "").strip().casefold() == (b or "").strip().casefold()
        if same(t.title, title) and same(t.artist, artist):
            return t
        if sources._similar(t.title, title) >= 0.85 and sources._similar(t.artist, artist) >= 0.7:
            return t
        return None

    def _get_editor_load(self, q):
        title, artist = self._song_args(q)
        text = lyrics_api.load_user_text(title, artist) if title else None
        return {"text": text, "saved": text is not None}

    def _post_editor_search(self, b):
        import requests
        title, artist = self._song_args(b)
        if not title:
            raise ValueError("Enter a title first.")
        t = self._matching_track(title, artist)
        try:
            rec = lyrics_api.search_online(title, artist, t.duration if t else 0, getattr(t, "album", "") if t else "")
        except requests.RequestException:
            return {"error": "Network error, unable to fetch lyrics"}
        if not rec:
            return {"found": False}
        synced = bool(rec.get("syncedLyrics"))
        return {"found": True, "synced": synced,
                "text": rec["syncedLyrics"] if synced else rec.get("plainLyrics", ""),
                "source": rec.get("source", "LRCLIB"), "track": rec.get("trackName"),
                "artist": rec.get("artistName")}

    def _post_editor_save(self, b):
        title, artist = self._song_args(b)
        text = str(b.get("text", ""))
        if not title or not text.strip():
            raise ValueError("Enter a title and some lyrics first.")
        t = self._matching_track(title, artist)
        path = lyrics_api.save_user(title, artist, text, t.album if t else "", t.duration if t else 0)
        self._run_on_app(self.app.lyrics_saved)
        lyr = lyrics_api.from_text(text)
        return {"file": path.name, "synced": bool(lyr and lyr.synced)}

    def _post_editor_delete(self, b):
        title, artist = self._song_args(b)
        if not lyrics_api.user_file(title, artist).exists():
            return {"deleted": False}
        lyrics_api.delete_user(title, artist)
        self._run_on_app(self.app.lyrics_saved)
        return {"deleted": True}

    def _get_editor_ai(self, q):
        return self.jobs.status()

    def _post_editor_ai_start(self, b):
        self.jobs.start(str(b.get("mode", "align")), [str(x) for x in b.get("lines", [])],
                        str(b.get("model", "small")), bool(b.get("restart", True)), self._song_args(b))
        return self.jobs.status()

    def _post_editor_ai_finish(self, b):
        self.jobs.finish()

    def _post_editor_ai_cancel(self, b):
        self.jobs.cancel()

    def _post_editor_slow(self, b):
        return {"message": self.jobs.slow_replay(float(b.get("start", 0)), float(b.get("end", 0)),
                                                 self._song_args(b))}

    # ---- AI usage ------------------------------------------------------------------
    def _get_usage(self, q):
        log = ai_translate.usage_log()
        month = time.strftime("%Y-%m")
        mon = [e for e in log if e.get("time", "").startswith(month)]
        return {
            "month": {"cost": sum(e["cost"] for e in mon), "songs": len(mon)},
            "all": {"cost": sum(e["cost"] for e in log), "songs": len(log),
                    "lines": sum(e.get("lines", 0) for e in log)},
            "rows": [{**e, "model_name": ai_translate.MODEL_NAMES.get(e.get("model"), e.get("model"))}
                     for e in reversed(log)],
        }
