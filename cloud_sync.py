"""Cloud sync to the phone: the PC puts its lyrics in a WebDAV folder, the Android app reads them.

Any WebDAV service works, e.g. free plans at Koofr (10 GB) or InfiniCLOUD (20 GB). You make an
app password on their site and enter the WebDAV address, user name and that password in both
apps. Only the PC writes; the phone only reads. Nothing else goes there.

What goes up: one small file, AutosyncedLyrics/sync.json.gz (gzip-compressed JSON):
    {"v": 1, "made": <unix time>, "songs": [
        {"ti": title, "ar": artist, "d": length in s, "mine": true if you synced it yourself,
         "synced": bool, "lines": [[ms, text, translation or null, 1 if AI else 0], ...]}]}
- your own lyrics (My Lyrics), which always win on the phone too
- lyrics found online (the lyrics cache), with their translations, including the AI /
  machine ones already made (so the phone doesn't pay for them again)

It's uploaded only when something changed: checked a minute after start, then every 10 minutes
(a few file dates compared, no reading). Building the file reads the cache once (~5 MB at most).
The password is kept encrypted for your Windows account, like the AI keys.
"""
import gzip
import json
import re
import threading
import time
import zlib

import ai_translate
import cache
import lyrics as lyrics_api
import paths

PASS_FILE = paths.DATA_DIR / "webdav_password.bin"
FOLDER = "AutosyncedLyrics"
FILE = "sync.json.gz"
CHECK_EVERY = 600          # s
FORMAT = 1

_lock = threading.Lock()
status = ""                # shown in Settings
_last_sig = None


# ---- the password (encrypted with Windows DPAPI, like the AI keys) -------------------
def set_password(pw: str | None) -> None:
    pw = (pw or "").strip()
    if not pw:
        PASS_FILE.unlink(missing_ok=True)
        return
    tmp = PASS_FILE.with_suffix(".tmp")
    tmp.write_bytes(ai_translate._dpapi(pw.encode("utf-8"), protect=True))
    tmp.replace(PASS_FILE)


def _password() -> str | None:
    try:
        return ai_translate._dpapi(PASS_FILE.read_bytes(), protect=False).decode("utf-8") or None
    except (OSError, ValueError):
        return None


def has_password() -> bool:
    return PASS_FILE.exists()


# ---- what goes up ----------------------------------------------------------------------
def _signature():
    """Changes whenever anything that goes up changes (file dates and sizes only)."""
    def stamp(p):
        try:
            s = p.stat()
            return (s.st_mtime_ns, s.st_size)
        except OSError:
            return None
    mine = sorted((f.name, stamp(f)) for f in lyrics_api.USER_DIR.glob("*.lrc")) \
        if lyrics_api.USER_DIR.exists() else []
    return (stamp(cache.FILE), stamp(ai_translate.STORE_FILE), tuple(mine))


def _ai_lines(title: str, artist: str, provider: str | None) -> dict[str, str]:
    """Translations the chosen service (Settings > Translated by) already made for this
    song; none when AI translation is off. Each service's are kept apart, as on the PC."""
    return ai_translate.known(title, artist, provider) if provider else {}


def build(provider: str | None = None) -> bytes:
    songs, seen = [], set()
    for s in lyrics_api.list_user():                       # your own lyrics
        f = lyrics_api.USER_DIR / s["file"]
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        lyr = lyrics_api.from_text(text)
        if not lyr:
            continue
        m = re.search(r"^\[length:\s*(\d+):(\d+)", text, re.M | re.I)
        songs.append({"ti": s["title"], "ar": s["artist"], "d": int(m[1]) * 60 + int(m[2]) if m else 0,
                      "mine": True, "synced": lyr.synced,
                      "lines": [[round(t * 1000), w, lyr.translations[i], 0] for i, (t, w) in enumerate(lyr.lines)]})
        seen.add((s["title"].strip().lower(), s["artist"].strip().lower()))
    for key, d, dur in _cached():                           # lyrics found online
        if key in seen or not d.get("lines"):
            continue
        title, artist = key
        ai = _ai_lines(title, artist, provider)
        tr = d.get("tr") or []
        lines = []
        for i, (t, w) in enumerate(d["lines"]):
            human = tr[i] if i < len(tr) else None
            got = human or ai.get(w)
            lines.append([round(t * 1000), w, got, 1 if got and not human else 0])
        songs.append({"ti": title, "ar": artist, "d": dur, "mine": False,
                      "synced": d.get("synced", True), "lines": lines})
    data = {"v": FORMAT, "made": int(time.time()), "songs": songs}
    return gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def _cached():
    """(title, artist) (lower case, as the cache keeps them), cache dict, length."""
    import sqlite3
    if not cache.FILE.exists():
        return []
    out = []
    try:
        with cache._db() as con:
            for key, blob in con.execute("SELECT key, data FROM songs").fetchall():
                try:
                    d = json.loads(zlib.decompress(blob))
                except (ValueError, zlib.error):
                    continue
                title, _, artist = key.partition("\n")
                out.append(((title, artist), d, d.get("duration") or 0))
    except sqlite3.Error:
        pass
    return out


# ---- WebDAV --------------------------------------------------------------------------------
def _target(url: str) -> tuple[str, str]:
    base = url.strip().rstrip("/")
    return f"{base}/{FOLDER}", f"{base}/{FOLDER}/{FILE}"


def upload(settings: dict, force: bool = False) -> str:
    """Puts the file up if anything changed (or force). Returns a status line."""
    global status, _last_sig
    import requests
    with _lock:
        url, user, pw = settings.get("webdav_url", ""), settings.get("webdav_user", ""), _password()
        if not (url and user and pw):
            status = "Enter the WebDAV address, user name and password."
            return status
        sig = (_signature(), settings.get("ai_translate"), settings.get("ai_provider"))
        if not force and sig == _last_sig:
            return status
        provider = settings.get("ai_provider", "claude") if settings.get("ai_translate") else None
        body = build(provider)
        folder, target = _target(url)
        auth = (user, pw)
        try:
            r = requests.request("MKCOL", folder, auth=auth, timeout=20)   # 405 = already there
            if r.status_code in (401, 403):
                status = "The cloud said no: check the user name and app password."
                return status
            r = requests.put(target, data=body, auth=auth, timeout=60,
                             headers={"Content-Type": "application/gzip"})
        except requests.RequestException as exc:
            status = f"Couldn't reach the cloud: {str(exc)[:100]}"
            return status
        if r.status_code not in (200, 201, 204):
            status = f"Upload failed ({r.status_code}). Check the WebDAV address."
            return status
        _last_sig = sig
        n = len(json.loads(gzip.decompress(body))["songs"])
        status = f"Synced {n} songs ({len(body) // 1024} KB) at {time.strftime('%H:%M')}."
        return status


def start(get_settings) -> None:
    """Background uploads while the app runs: a minute after start, then every 10 minutes."""
    def loop():
        time.sleep(60)
        while True:
            s = get_settings()
            if s.get("cloud_sync"):
                try:
                    upload(s)
                except Exception as exc:              # never take the app down
                    global status
                    status = f"Sync failed: {str(exc)[:100]}"
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, daemon=True).start()
