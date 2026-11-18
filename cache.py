"""A small, size-capped cache of lyrics found online, so replayed songs show up instantly.

One compressed file (lyrics_cache.db in the AppData folder), at most MAX_BYTES; when it's
full the songs not played for the longest are dropped. Your own synced lyrics (my_lyrics)
are separate and never touched by this.

    found lyrics          kept 60 days, then fetched fresh (sources improve)
    "no lyrics found"     kept 3 days, so new uploads get picked up
    no translation found  lyrics kept, translation looked for again after 2 days
                          (e.g. NetEase was blocked on the school network)
    network errors        never cached
"""
import json
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager

import paths

FILE = paths.DATA_DIR / "lyrics_cache.db"
MAX_BYTES = 5 * 1024 * 1024
KEEP_FOUND = 60 * 86400
KEEP_MISSING = 3 * 86400
RECHECK_EXTRAS = 2 * 86400
LENGTH_TOLERANCE = 3          # seconds; a different length is probably another version

_lock = threading.Lock()


@contextmanager
def _db():
    """One connection, committed and closed afterwards (called from several threads)."""
    with _lock:
        con = sqlite3.connect(FILE, timeout=5)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS songs (key TEXT PRIMARY KEY, data BLOB, "
                        "saved REAL, used REAL)")
            yield con
            con.commit()
        finally:
            con.close()


def _key(title: str, artist: str) -> str:
    return f"{title.strip().lower()}\n{artist.strip().lower()}"


def get(title: str, artist: str, duration: float):
    """(found, lyrics_dict_or_None). found=False means "not cached, go and look"."""
    try:
        with _db() as con:
            row = con.execute("SELECT data, saved FROM songs WHERE key=?",
                              (_key(title, artist),)).fetchone()
            if not row:
                return False, None
            d = json.loads(zlib.decompress(row[0]))
            age = time.time() - row[1]
            if age > (KEEP_FOUND if d.get("lines") is not None else KEEP_MISSING):
                return False, None
            if duration and d.get("duration") and abs(d["duration"] - duration) > LENGTH_TOLERANCE:
                return False, None
            if d.get("extras") and not any(d.get("tr") or []) and \
                    time.time() - d.get("extras_at", 0) > RECHECK_EXTRAS:
                d["extras"] = False                       # look for a translation again
            con.execute("UPDATE songs SET used=? WHERE key=?", (time.time(), _key(title, artist)))
            return True, d
    except (sqlite3.Error, OSError, ValueError, zlib.error):
        return False, None


def put(title: str, artist: str, duration: float, d: dict | None, keep_age: bool = False) -> None:
    """Save lyrics (d) or a "nothing found" (d=None), then trim to MAX_BYTES.

    keep_age: an update (translation added), so the 60 days still count from the first save.
    """
    d = dict(d or {"lines": None})
    d["duration"] = round(duration or 0, 1)
    blob = zlib.compress(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode(), 9)
    now = time.time()
    try:
        with _db() as con:
            old = con.execute("SELECT saved FROM songs WHERE key=?", (_key(title, artist),)).fetchone()
            saved = old[0] if old and keep_age else now
            con.execute("INSERT OR REPLACE INTO songs VALUES (?,?,?,?)",
                        (_key(title, artist), blob, saved, now))
            _trim(con)
    except (sqlite3.Error, OSError):
        pass


def _trim(con) -> None:
    total = con.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM songs").fetchone()[0]
    if total <= MAX_BYTES * 0.9:
        return
    for key, size in con.execute("SELECT key, LENGTH(data) FROM songs ORDER BY used").fetchall():
        if total <= MAX_BYTES * 0.7:
            break
        con.execute("DELETE FROM songs WHERE key=?", (key,))
        total -= size
    con.commit()
    con.execute("VACUUM")                       # give the space back to the disk


def size() -> int:
    """Bytes the cache takes on disk."""
    try:
        return FILE.stat().st_size
    except OSError:
        return 0


def count() -> int:
    """Songs in the cache (including ones remembered as "no lyrics found")."""
    if not FILE.exists():
        return 0
    try:
        with _db() as con:
            return con.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    except sqlite3.Error:
        return 0


def clear() -> None:
    with _lock:
        try:
            FILE.unlink(missing_ok=True)
        except OSError:
            pass
