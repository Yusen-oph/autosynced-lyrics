"""Extra lyric sources, tried only when LRCLIB has no synced lyrics for a song.

Every source returns the same record shape LRCLIB uses:
{"trackName", "artistName", "albumName", "duration", "syncedLyrics", "plainLyrics", "source"}

NetEase Cloud Music and QQ Music are blocked on some networks (e.g. a school web filter).
They never raise: if one fails it's skipped for a while and the others are used.

Not included, on purpose:
- Musixmatch: its free access means posing as their desktop app, against their terms.
"""
import base64
import html
import json
import time
import re
import unicodedata
import zlib
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

from functools import lru_cache

import requests

HEADERS = {"User-Agent": "autosynced-lyrics/0.1"}

# One shared session: connections to each site stay open between requests, so after the
# first song a lookup skips the connection setup (~1.2 s -> ~0.4 s per request to NetEase).
http = requests.Session()
http.mount("https://", requests.adapters.HTTPAdapter(pool_connections=12, pool_maxsize=12))
# Lookups run side by side here. Only the network calls themselves go in the pool (nothing
# in it waits for another job in it), so it can't jam up when songs are skipped quickly.
pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="lyrics")
TIMEOUT = 8
MAX_LENGTH_DIFF = 10  # seconds; more than this is probably another version of the song

_CREDIT = re.compile(
    r"(作词|作曲|编曲|制作人|监制|混音|母带|和声|演唱|原唱|填词|"
    r"lyrics? by|written by|composed by|composer|lyricist|producer|produced by|arranged by|"
    r"music by|words by)\s*[:：]", re.I)
_TIMED = re.compile(r"^((?:\[\d+:\d+(?:[.:]\d+)?\])+)(.*)$")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"\s*[-(\[].*?(remaster|live|version|edit|mix|feat|ft\.|mono|stereo).*$", "", s)
    return re.sub(r"[\W_]+", "", s)


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def strip_credits(lrc: str, title: str, artist: str) -> str:
    """Drop credit lines (lyricist, composer…) and a 'Title - Artist' line near the start."""
    header = _norm(f"{title}{artist}")
    out = []
    for line in lrc.splitlines():
        m = _TIMED.match(line.strip())
        if m:
            words = m.group(2).strip()
            if _CREDIT.search(words):
                continue
            if words and _norm(words.replace(" - ", "")) in (header, _norm(f"{artist}{title}")):
                continue
        out.append(line)
    return "\n".join(out)


# ---- Kugou: large catalogue of synced lyrics (English, Chinese, Japanese, Korean…) ----
def _kugou_candidates(title: str, artist: str, duration: float) -> list[dict]:
    r = http.get("https://lyrics.kugou.com/search", headers=HEADERS, timeout=TIMEOUT, params={
        "ver": 1, "man": "yes", "client": "pc",
        "keyword": f"{artist} - {title}", "duration": int(duration * 1000)})
    r.raise_for_status()
    candidates = r.json().get("candidates") or []

    def fits(c):
        length_ok = not duration or abs(c.get("duration", 0) / 1000 - duration) <= MAX_LENGTH_DIFF
        return length_ok and _similar(c.get("song", ""), title) >= 0.6 \
            and _similar(c.get("singer", ""), artist) >= 0.5

    return sorted(filter(fits, candidates), key=lambda c: -c.get("score", 0))[:3]


def kugou(title: str, artist: str, duration: float) -> dict | None:
    for c in _kugou_candidates(title, artist, duration):
        d = http.get("https://lyrics.kugou.com/download", headers=HEADERS, timeout=TIMEOUT,
                         params={"ver": 1, "client": "pc", "id": c["id"], "accesskey": c["accesskey"],
                                 "fmt": "lrc", "charset": "utf8"})
        if not d.ok:
            continue
        lrc = base64.b64decode(d.json().get("content") or "").decode("utf-8", "replace")
        lrc = strip_credits(lrc, title, artist)
        words = [m.group(2).strip() for l in lrc.splitlines() if (m := _TIMED.match(l.strip()))]
        if sum(1 for w in words if w) < 3:
            continue  # empty or near-empty file
        return {"trackName": c.get("song") or title, "artistName": c.get("singer") or artist,
                "albumName": "", "duration": c.get("duration", 0) / 1000,
                "syncedLyrics": lrc, "plainLyrics": None, "source": "Kugou"}
    return None


# ---- Kugou KRC: per-word timing + Chinese translation ---------------------------
_KRC_KEY = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])
_KRC_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_KRC_WORD = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")


def parse_krc(text: str) -> dict:
    """{"lines": [(start_s, end_s, text, [(char_offset, start_s, end_s), …])], "translations": [str|None]}"""
    m = re.search(r"^\[offset:\s*([+-]?\d+)\s*\]", text, re.M | re.I)
    shift = int(m.group(1)) / 1000 if m else 0.0
    lines = []
    for raw in text.splitlines():
        lm = _KRC_LINE.match(raw.strip())
        if not lm:
            continue
        start, length = int(lm.group(1)) / 1000 - shift, int(lm.group(2)) / 1000
        words, line_text = [], ""
        for wm in _KRC_WORD.finditer(lm.group(3)):
            off, dur = int(wm.group(1)) / 1000, int(wm.group(2)) / 1000
            words.append((len(line_text), start + off, start + off + dur))
            line_text += wm.group(3)
        lines.append((start, start + length, line_text, words))
    translations = [None] * len(lines)
    lm = re.search(r"^\[language:([A-Za-z0-9+/=]+)\]", text, re.M)
    if lm:
        try:
            blocks = json.loads(base64.b64decode(lm.group(1)))["content"]
            tr = next((b["lyricContent"] for b in blocks if b.get("type") == 1), None) or []
            for i, item in enumerate(tr[:len(lines)]):
                t = "".join(item).strip() if item else ""
                translations[i] = t or None
        except (ValueError, KeyError, TypeError):
            pass
    return {"lines": lines, "translations": translations}


def kugou_krc(title: str, artist: str, duration: float) -> dict | None:
    """Word timings (and translations, if Kugou has them) for a song; None if not found."""
    for c in _kugou_candidates(title, artist, duration):
        d = http.get("https://lyrics.kugou.com/download", headers=HEADERS, timeout=TIMEOUT,
                         params={"ver": 1, "client": "pc", "id": c["id"], "accesskey": c["accesskey"],
                                 "fmt": "krc", "charset": "utf8"})
        if not d.ok:
            continue
        raw = base64.b64decode(d.json().get("content") or "")
        if raw[:4] != b"krc1":
            continue
        try:
            text = zlib.decompress(bytes(b ^ _KRC_KEY[i % 16] for i, b in enumerate(raw[4:])))
        except zlib.error:
            continue
        krc = parse_krc(text.decode("utf-8", "replace"))
        if len(krc["lines"]) >= 3:
            return krc
    return None


# ---- NetEase Cloud Music / QQ Music: big catalogues, good Chinese translations ----
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
QUICK = (3, 4)          # seconds to connect / to answer; these may be blocked, so don't wait long
TRANSLATION_WAIT = 4    # after this, go with Kugou's translation (a slow site finishes unseen)
RETRY_AFTER = 600       # after a failure, leave that source alone for 10 minutes
_down_until: dict[str, float] = {}


def _guarded(name: str, fn, *args):
    """Run fn; on any failure return None and skip this source for a while."""
    if time.monotonic() < _down_until.get(name, 0):
        return None
    for attempt in range(2):                 # one retry: QQ sometimes drops a connection
        try:
            return fn(*args)
        except requests.Timeout:
            break                            # slow, not flaky: a retry would only add waiting
        except requests.RequestException:
            if attempt:
                break
        except Exception:
            break                            # unexpected answer: retrying won't help
    _down_until[name] = time.monotonic() + RETRY_AFTER
    return None


def _pick(songs: list[tuple], title: str, artist: str, duration: float) -> list:
    """songs: [(id, name, artists, seconds)] -> ids of the best few matches.

    The artist may be written differently (周杰倫 / Jay Chou), so a near-exact title with
    the same length is also accepted.
    """
    out = []
    for sid, name, artists, secs in songs:
        length_ok = not duration or not secs or abs(secs - duration) <= MAX_LENGTH_DIFF
        t, a = _similar(name, title), _similar(artists, artist)
        same_len = bool(duration and secs and abs(secs - duration) <= 3)
        if length_ok and t >= 0.6 and (a >= 0.5 or (t >= 0.9 and same_len)):
            out.append((-(t + a), abs(secs - duration) if duration and secs else 0, sid))
    return [sid for *_, sid in sorted(out, key=lambda x: x[:2])[:3]]


def _lrc_lines(lrc: str) -> list[tuple[float, str]]:
    lines = []
    for raw in (lrc or "").splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        words = re.sub(r"\[\d+:\d+(?:\.\d+)?\]", "", raw).strip()
        lines += [(int(m) * 60 + float(sec), words) for m, sec in stamps]
    return sorted(lines)


def _as_extras(lrc: str, tlrc: str) -> dict | None:
    """Lyrics + translation LRC -> the same shape as parse_krc() (no word timing)."""
    lines = [l for l in _lrc_lines(lrc) if l[1]]
    tr = {round(t, 2): w for t, w in _lrc_lines(tlrc) if w and w not in ("//", "/")}
    if len(lines) < 3 or not tr:
        return None
    rows = [(t, lines[i + 1][0] if i + 1 < len(lines) else t + 5, w, [])
            for i, (t, w) in enumerate(lines)]
    trs = [tr.get(round(t, 2)) for t, *_ in rows]
    return {"lines": rows, "translations": trs} if sum(1 for x in trs if x) >= 3 else None


def _record(lrc: str, title, artist, secs, source) -> dict | None:
    lrc = strip_credits(lrc or "", title, artist)
    if sum(1 for _, w in _lrc_lines(lrc) if w) < 3:
        return None
    return {"trackName": title, "artistName": artist, "albumName": "", "duration": secs or 0,
            "syncedLyrics": lrc, "plainLyrics": None, "source": source}


# NetEase
def _netease_search(title: str, artist: str, duration: float) -> list:
    r = http.get("https://music.163.com/api/search/get", timeout=QUICK,
                     headers={**BROWSER, "Referer": "https://music.163.com/"},
                     params={"s": f"{title} {artist}", "type": 1, "limit": 10, "offset": 0})
    r.raise_for_status()
    songs = (r.json().get("result") or {}).get("songs") or []
    return _pick([(s["id"], s.get("name", ""), " ".join(a.get("name", "") for a in s.get("artists", [])),
                   (s.get("duration") or 0) / 1000) for s in songs], title, artist, duration)


@lru_cache(maxsize=32)
def _netease_lyric(song_id) -> tuple[str, str]:
    r = http.get("https://music.163.com/api/song/lyric", timeout=QUICK,
                     headers={**BROWSER, "Referer": "https://music.163.com/"},
                     params={"id": song_id, "lv": -1, "tv": -1})
    r.raise_for_status()
    j = r.json()
    return (j.get("lrc") or {}).get("lyric") or "", (j.get("tlyric") or {}).get("lyric") or ""


# QQ Music
def _qq_search(title: str, artist: str, duration: float) -> list:
    r = http.get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp", timeout=QUICK,
                     headers={**BROWSER, "Referer": "https://y.qq.com/"},
                     params={"w": f"{title} {artist}", "format": "json", "p": 1, "n": 10,
                             "cr": 1, "new_json": 1})
    r.raise_for_status()
    songs = ((r.json().get("data") or {}).get("song") or {}).get("list") or []
    if not songs:
        return _qq_search_desktop(title, artist, duration)
    return _pick([(s.get("mid") or s.get("songmid"), s.get("title") or s.get("songname", ""),
                   " ".join(a.get("name", "") for a in s.get("singer", [])), s.get("interval") or 0)
                  for s in songs], title, artist, duration)


def _qq_search_desktop(title: str, artist: str, duration: float) -> list:
    body = {"req": {"module": "music.search.SearchCgiService", "method": "DoSearchForQQMusicDesktop",
                    "param": {"query": f"{title} {artist}", "num_per_page": 10, "page_num": 1,
                              "search_type": 0}}}
    r = http.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=body, timeout=QUICK,
                      headers={**BROWSER, "Referer": "https://y.qq.com/"})
    r.raise_for_status()
    songs = r.json()["req"]["data"]["body"]["song"]["list"]
    return _pick([(s["mid"], s.get("title") or s.get("name", ""),
                   " ".join(a.get("name", "") for a in s.get("singer", [])), s.get("interval") or 0)
                  for s in songs], title, artist, duration)


def _qq_text(v: str) -> str:
    v = v or ""
    if v and "[" not in v:                         # base64 when nobase64 isn't honoured
        v = base64.b64decode(v).decode("utf-8", "replace")
    return html.unescape(v)


@lru_cache(maxsize=32)
def _qq_lyric(mid: str) -> tuple[str, str]:
    """(lyrics, Chinese translation) as LRC text; the translation may be empty."""
    body = {"comm": {"ct": 24, "cv": 0},
            "req": {"module": "music.musichallSong.PlayLyricInfo", "method": "GetPlayLyricInfo",
                    "param": {"songMID": mid, "trans": 1, "roma": 0, "crypt": 0, "type": 0}}}
    r = http.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=body, timeout=QUICK,
                      headers={**BROWSER, "Referer": "https://y.qq.com/"})
    r.raise_for_status()
    d = r.json()["req"]["data"]
    return _qq_text(d.get("lyric")), _qq_text(d.get("trans"))


_SITES = {"NetEase": (_netease_search, _netease_lyric), "QQ Music": (_qq_search, _qq_lyric)}


def _synced(name: str, title: str, artist: str, duration: float) -> dict | None:
    search, lyric = _SITES[name]
    for sid in search(title, artist, duration):
        rec = _record(lyric(sid)[0], title, artist, duration, name)
        if rec:
            return rec
    return None


def _translation(name: str, title: str, artist: str, duration: float) -> dict | None:
    search, lyric = _SITES[name]
    for sid in search(title, artist, duration):
        found = _as_extras(*lyric(sid))
        if found:
            return found
    return None


def netease(title: str, artist: str, duration: float) -> dict | None:
    return _guarded("NetEase", _synced, "NetEase", title, artist, duration)


def qq_music(title: str, artist: str, duration: float) -> dict | None:
    return _guarded("QQ Music", _synced, "QQ Music", title, artist, duration)


def translation(title: str, artist: str, duration: float) -> dict | None:
    """Human Chinese translation lined up with its lyrics, or None.

    NetEase and QQ Music are asked at the same time; NetEase's answer is preferred.
    """
    return pick_translation(translation_jobs(title, artist, duration))


def translation_jobs(title: str, artist: str, duration: float) -> list:
    return [pool.submit(_guarded, name, _translation, name, title, artist, duration) for name in _SITES]


def pick_translation(jobs: list) -> dict | None:
    """The first site's translation if it has one, else the next's; None after TRANSLATION_WAIT."""
    deadline = time.monotonic() + TRANSLATION_WAIT
    for job in jobs:
        try:
            found = job.result(timeout=max(0.0, deadline - time.monotonic()))
        except TimeoutError:
            return None
        if found:
            return found
    return None


# ---- lyrics.ovh: untimed lyrics only (a starting point for Whisper / tap-sync) ----
def lyrics_ovh(title: str, artist: str) -> dict | None:
    clean = re.sub(r"\s*[-(\[].*?(remaster|live|version|edit|mix|feat).*$", "", title, flags=re.I)
    r = http.get(f"https://api.lyrics.ovh/v1/{requests.utils.quote(artist)}/{requests.utils.quote(clean)}",
                     headers=HEADERS, timeout=TIMEOUT)
    if not r.ok:
        return None
    text = (r.json().get("lyrics") or "").replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[0].lower().startswith("paroles de la chanson"):
        lines = lines[1:]           # French site header that sometimes leaks in
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        return None
    return {"trackName": title, "artistName": artist, "albumName": "", "duration": 0,
            "syncedLyrics": None, "plainLyrics": text, "source": "lyrics.ovh"}
