"""Finds time-synced lyrics (LRCLIB first, then other sources) and parses LRC."""
import re
import time
from bisect import bisect_right
from pathlib import Path

import requests

import cache
import paths
import sources

API = "https://lrclib.net/api"
HEADERS = {"User-Agent": "autosynced-lyrics/0.1"}

_TIMESTAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
# " - Remastered 2011", " (feat. X)", " - Live" etc. hurt matching
_NOISE = re.compile(r"\s*[-(\[].*?(remaster|live|version|edit|mix|feat\.|mono|stereo).*$", re.I)


class Lyrics:
    def __init__(self, lines: list[tuple[float, str]], synced: bool):
        self.lines = lines
        self.synced = synced
        self._times = [t for t, _ in lines]
        # Filled in by attach_krc(): per line, a Chinese translation and per-character timing.
        self.translations: list[str | None] = [None] * len(lines)
        self.char_times: list[list[tuple[float, float]] | None] = [None] * len(lines)
        self.extras_loaded = False
        self.online = False          # found online (so it may be cached), not one of yours
        self.ai_lines: set[int] = set()   # translations written by Claude (see ai_translate)
        self.ai_checked = False
        # Fixing lyrics timed for another version of the song, by listening (see retime()):
        self.heard_starts: dict[int, float] = {}   # line index -> when it was actually sung
        self.heard_lines: set[int] = set()         # lines whose word timing came from listening
        self.orig_times: list[float] | None = None # the file's own times, before any fix
        self.orig_chars = None
        self.retimed = False                       # lines have been moved to match the song
        self.retime_done = False                   # no need to measure again (e.g. from cache)

    def set_times(self, times: list[float]) -> None:
        self.lines = [(t, w) for t, (_, w) in zip(times, self.lines)]
        self._times = list(times)
        self._shown_key = None

    def index_at(self, seconds: float) -> int:
        """Index of the line that should be showing at `seconds` (-1 = before first line)."""
        return bisect_right(self.shown_times(), seconds) - 1

    HOLD_AFTER_LAST_WORD = 0.3   # s a line stays after its last word ends, before a music break
    MAX_HOLD = 4.0               # never hold more than this past the file's own mark

    def shown_times(self) -> list[float]:
        """When each line takes over the screen. Same as the file, except the empty line that
        marks "singing stops here": files often put it where the last word *starts*, so a held
        note vanished early. It now waits until the previous line's last word has ended
        (from word timing; a small grace without it), never running into the next line."""
        key = sum(1 for c in self.char_times if c)
        if getattr(self, "_shown_key", None) == key:
            return self._shown
        times = list(self._times)
        for i in range(1, len(self.lines)):
            if self.lines[i][1].strip() or not self.lines[i - 1][1].strip():
                continue                       # only an empty line right after a sung line
            mark = times[i]
            chars = self.char_times[i - 1]
            ends = [c[1] for c in chars if c] if chars else []
            hold_to = (max(ends) if ends else mark) + self.HOLD_AFTER_LAST_WORD
            limit = min(mark + self.MAX_HOLD,
                        (self._times[i + 1] - 0.2) if i + 1 < len(self.lines) else mark + self.MAX_HOLD)
            times[i] = max(mark, min(hold_to, limit))
        self._shown_key, self._shown = key, times
        return times

    @property
    def has_translation(self) -> bool:
        return any(self.translations)

    @property
    def is_chinese(self) -> bool:
        """Mostly Chinese characters and no kana/hangul: no translation needed."""
        text = "".join(t for _, t in self.lines)
        han = len(re.findall(r"[一-鿿]", text))
        if re.search(r"[぀-ヿ가-힯]", text):
            return False
        letters = len(re.findall(r"\w", text))
        return letters > 0 and han / letters > 0.3


# ---- translations and word timing from Kugou's KRC format --------------------
def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def attach_krc(lyr: Lyrics, krc: dict | None) -> None:
    """Match KRC lines to our lines by text + time; copy translations and word timing."""
    lyr.extras_loaded = True
    if not krc or not lyr.synced:
        return
    from difflib import SequenceMatcher
    k_lines, k_tr = krc["lines"], krc["translations"]

    def score(js, key, t):
        kk = "".join(_squash(k_lines[j][2]) for j in js)
        s = 1.0 if kk == key else SequenceMatcher(None, kk, key).ratio()
        return s - abs(k_lines[js[0]][0] - t) / 30     # prefer the closer one when texts tie

    timed = []                                     # lines that got word timing here
    for i, (t, text) in enumerate(lyr.lines):
        key = _squash(text)
        if not key:
            continue
        t_next = lyr.lines[i + 1][0] if i + 1 < len(lyr.lines) else t + 15
        # Candidates: one nearby Kugou line, or a run of consecutive ones (Kugou often
        # splits a long line in two).
        options = [[j] for j, k in enumerate(k_lines) if abs(k[0] - t) <= 3.0]
        run = [j for j, k in enumerate(k_lines) if t - 1.0 <= k[0] < t_next - 0.5]
        if len(run) > 1 and run == list(range(run[0], run[-1] + 1)):
            options.append(run)
        if not options:
            continue
        best = max(options, key=lambda js: score(js, key, t))
        if score(best, key, t) < 0.75:
            # Maybe the other way round: one of their lines = this line + our next one.
            # Then the translation goes under this (first) line.
            if i + 1 < len(lyr.lines) and not lyr.translations[i]:
                pair = key + _squash(lyr.lines[i + 1][1])
                near = [[j] for j, k in enumerate(k_lines) if abs(k[0] - t) <= 3.0]
                hit = max(near, key=lambda js: score(js, pair, t))
                if score(hit, pair, t) >= 0.75 and hit[0] < len(k_tr) and k_tr[hit[0]] \
                        and _squash(k_tr[hit[0]]) != pair:
                    lyr.translations[i] = k_tr[hit[0]].rstrip("，,。 ")
            continue
        ktext, kwords, offset = "", [], 0
        for j in best:
            ks, ke, jt, jw = k_lines[j]
            if ktext:
                ktext += " "
            offset = len(ktext)
            kwords += [(offset + o, s, e) for o, s, e in jw]
            ktext += jt
        trs = [k_tr[j] for j in best if j < len(k_tr) and k_tr[j]]
        tr = "，".join(x.rstrip("，,。 ") for x in trs) if trs else None
        if tr and _squash(tr) != key:
            lyr.translations[i] = tr
        if kwords:                                 # NetEase/QQ translations carry no word timing
            lyr.char_times[i] = _map_chars(text, ktext, kwords, 0.0)
            if lyr.char_times[i]:
                timed.append(i)
    _align_word_timing(lyr, timed)


NATURAL_LEAD = 0.05   # lyric lines usually start this much before the first word is sung


def _align_word_timing(lyr: Lyrics, timed: list[int]) -> None:
    """Kugou's copy of a song can be timed a little differently from the lyrics on screen
    (another master / intro length): its words then all come e.g. 0.7 s late. If its first
    words are consistently off from our line starts, move all its word times to match."""
    diffs = []
    for i in timed:
        first = next((c[0] for c in lyr.char_times[i] if c), None)
        if first is not None:
            diffs.append(first - lyr.lines[i][0])
    if len(diffs) < 5:
        return
    diffs.sort()
    off = diffs[len(diffs) // 2]
    if abs(off) <= 0.25:                         # within normal variation: leave it
        return
    shift = NATURAL_LEAD - off
    for i in timed:
        lyr.char_times[i] = [(c[0] + shift, c[1] + shift) if c else None
                             for c in lyr.char_times[i]]


def fetch_extras(title: str, artist: str, duration: float):
    """Start looking up word timing (Kugou) and translations (NetEase / QQ Music), all at once.
    Returns a handle; its result() goes to apply_extras()."""
    def kugou():
        try:
            return sources.kugou_krc(title, artist, duration)
        except Exception:
            return None
    return _Extras(sources.pool.submit(kugou), sources.translation_jobs(title, artist, duration))


class _Extras:
    """Lookups in progress; result() waits for them (in the caller's thread, not the pool's)."""
    def __init__(self, kugou_job, translation_jobs):
        self.k, self.t = kugou_job, translation_jobs

    def result(self):
        return self.k.result(), sources.pick_translation(self.t)


def apply_extras(lyr: Lyrics, found) -> None:
    """Word timing from Kugou; translations from NetEase / QQ Music when found, else Kugou."""
    krc, tr = found
    attach_krc(lyr, krc)
    attach_krc(lyr, tr)
    lyr.extras_loaded = True


def attach_extras(lyr: Lyrics, title: str, artist: str, duration: float) -> None:
    apply_extras(lyr, fetch_extras(title, artist, duration).result())


def _map_chars(ours: str, theirs: str, words, shift: float):
    """Per character of `ours`: (start, end) taken from KRC word timing.

    The two texts often differ a little ("Oh, I'm in love" / "I'm in love", "goin'" /
    "going", a singer's name in front): letters are lined up loosely, and only those
    that match get timing. None if the texts are too different to trust.
    """
    from difflib import SequenceMatcher
    per_char = [None] * len(theirs)
    for n, (off, start, end) in enumerate(words):
        nxt = words[n + 1][0] if n + 1 < len(words) else len(theirs)
        for c in range(off, nxt):
            per_char[c] = (start + shift, end + shift)
    a_idx = [k for k, ch in enumerate(ours) if ch.isalnum()]
    b_idx = [k for k, ch in enumerate(theirs) if ch.isalnum()]
    a = "".join(ours[k].lower() for k in a_idx)
    b = "".join(theirs[k].lower() for k in b_idx)
    if not a or not b:
        return None
    sm = SequenceMatcher(None, a, b, autojunk=False)
    blocks = sm.get_matching_blocks()
    matched = sum(size for _, _, size in blocks)
    if matched < 0.7 * len(a):
        return None                                # too different: estimate instead
    out = [None] * len(ours)
    for i, j, size in blocks:
        for k in range(size):
            out[a_idx[i + k]] = per_char[b_idx[j + k]]
    return out


def _fill_gaps(hits: list, t0: float, end: float) -> list[float]:
    """Words without their own timing (an extra "Oh", a changed word) get times spread
    evenly between the timed words around them."""
    out = list(hits)
    k = 0
    while k < len(out):
        if out[k] is not None:
            k += 1
            continue
        j = k
        while j < len(out) and out[j] is None:
            j += 1
        before = out[k - 1] if k else None
        after = out[j] if j < len(out) else None
        lo = before if before is not None else (min(t0, after - 0.2 * (j - k)) if after is not None else t0)
        hi = after if after is not None else max(lo + 0.25 * (j - k + 1), min(end, lo + 0.3 * (j - k + 1)))
        if before is None and after is not None:
            lo = max(t0, after - 0.25 * (j - k))   # lead-in words just before the first timed one
        n = j - k + (1 if before is not None else 0)
        for m in range(k, j):
            step = (m - k + (1 if before is not None else 0)) / max(1, n)
            out[m] = lo + (hi - lo) * step
        k = j
    return out


def has_word_timing(lyr: Lyrics, i: int) -> bool:
    """True when line i has real per-word timing (else word_times() is an estimate)."""
    return 0 <= i < len(lyr.char_times) and bool(lyr.char_times[i])


def word_times(lyr: Lyrics, i: int, tokens: list[tuple[int, str]]) -> tuple[list[float], float]:
    """Start time of each displayed word (token = (char offset, text)), and when singing ends."""
    t0 = lyr.lines[i][0]
    t_next = lyr.lines[i + 1][0] if i + 1 < len(lyr.lines) else t0 + 6.0
    chars = lyr.char_times[i]
    if chars:
        hits = []
        for off, tok in tokens:
            hit = next((chars[off + k] for k in range(len(tok)) if off + k < len(chars) and chars[off + k]), None)
            hits.append(hit[0] if hit else None)
        ends = [c[1] for c in chars if c]
        end = min(max(ends) if ends else t_next, t_next)
        starts, last = [], t0
        for s in _fill_gaps(hits, t0, end):       # inside this line, never going backwards
            last = max(last, min(s, t_next - 0.05))
            starts.append(last)
        return starts, min(t_next, max(end, starts[-1] + 0.15)) if starts else end
    # Estimate: spread the words over a typical sung length, weighted by word length.
    n = len(tokens)
    if n == 0:
        return [], t0
    sung = min(t_next - t0 - 0.15, 0.36 * n + 0.6)
    sung = max(0.4, sung)
    weights = [1 + len(tok) / 5 for _, tok in tokens]
    total, acc, starts = sum(weights), 0.0, []
    for w in weights:
        starts.append(t0 + sung * acc / total)
        acc += w
    return starts, t0 + sung


def parse_lrc(text: str) -> list[tuple[float, str]]:
    # [offset:+200] = show lyrics 200 ms sooner (standard LRC tag, used by Kugou files)
    m = re.search(r"^\[offset:\s*([+-]?\d+)\s*\]", text, re.M | re.I)
    shift = int(m.group(1)) / 1000 if m else 0.0
    lines = []
    for raw in text.splitlines():
        stamps = _TIMESTAMP.findall(raw)
        if not stamps:
            continue
        words = _TIMESTAMP.sub("", raw).strip()
        for mins, secs in stamps:
            lines.append((max(0.0, int(mins) * 60 + float(secs) - shift), words))
    lines.sort(key=lambda x: x[0])     # stable: file order kept within a timestamp
    # Two lines on one timestamp = the original plus a romanisation/translation of it
    # (only the last would ever show): keep the original.
    out = []
    for t, w in lines:
        if out and abs(out[-1][0] - t) < 0.005 and out[-1][1]:
            continue
        out.append((t, w))
    return out


def _from_record(rec: dict) -> Lyrics | None:
    if rec.get("syncedLyrics"):
        return Lyrics(parse_lrc(rec["syncedLyrics"]), synced=True)
    if rec.get("plainLyrics"):
        return Lyrics([(0.0, l) for l in rec["plainLyrics"].splitlines()], synced=False)
    return None


def _query(title: str, artist: str, album: str, duration: float) -> dict | None:
    """LRCLIB first, exactly as before. Other sources only if LRCLIB has no synced lyrics."""
    lrclib_error = None
    try:
        rec = _lrclib(title, artist, album, duration)
    except (requests.RequestException, ValueError) as exc:
        rec, lrclib_error = None, exc
    if rec and rec.get("syncedLyrics"):
        return {**rec, "source": "LRCLIB"}
    for find in (sources.netease, sources.qq_music, sources.kugou):
        try:
            found = find(title, artist, duration)
        except (requests.RequestException, ValueError, KeyError):
            continue  # that source is down or blocked: try the next one
        if found:
            return found
    if rec:
        return {**rec, "source": "LRCLIB"}       # untimed lyrics from LRCLIB
    if lrclib_error:
        raise lrclib_error
    return None


def _rank(results: list, title: str, artist: str, duration: float) -> list:
    """LRCLIB search results, best first: synced, then the closest length. Results by
    someone else (a different song with the same name) and ones more than 15 s off the
    track's length are dropped."""
    def fits(x):
        if sources._similar(x.get("trackName", ""), title) < 0.6:
            return False
        if sources._similar(x.get("artistName", ""), artist) < 0.6:
            return False
        return not duration or abs((x.get("duration") or 0) - duration) <= 15
    keep = [x for x in results if (x.get("syncedLyrics") or x.get("plainLyrics")) and fits(x)]
    keep.sort(key=lambda x: (not x.get("syncedLyrics"),
                             abs((x.get("duration") or 0) - duration) if duration else 0))
    return keep


def _closer_version(exact: dict, title: str, artist: str, duration: float) -> dict:
    """LRCLIB often has several synced versions of a song. If the one it picked is more
    than a second longer/shorter than the track playing (probably another edit, so every
    line would be shifted), use a synced version whose length matches better."""
    off = abs((exact.get("duration") or 0) - duration) if duration else 0
    if off <= 1.0:
        return exact
    clean = _NOISE.sub("", title).strip() or title
    try:
        r = sources.http.get(f"{API}/search", params={"track_name": clean, "artist_name": artist},
                             headers=HEADERS, timeout=10)
        results = r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        return exact
    better = [x for x in results if x.get("syncedLyrics")
              and sources._similar(x.get("trackName", ""), title) >= 0.8
              and sources._similar(x.get("artistName", ""), artist) >= 0.6
              and abs((x.get("duration") or 0) - duration) < off - 0.5]
    return min(better, key=lambda x: abs(x["duration"] - duration)) if better else exact


def _lrclib(title: str, artist: str, album: str, duration: float) -> dict | None:
    params = {"track_name": title, "artist_name": artist, "album_name": album,
              "duration": round(duration)}
    r = sources.http.get(f"{API}/get", params=params, headers=HEADERS, timeout=10)
    exact = r.json() if r.ok else None
    if exact and exact.get("syncedLyrics"):
        return _closer_version(exact, title, artist, duration)
    if exact and not exact.get("plainLyrics"):
        exact = None

    # No synced lyrics from the exact match (none at all, or only untimed ones):
    # fuzzy search, prefer synced results with the closest duration.
    clean = _NOISE.sub("", title).strip() or title
    try:
        r = sources.http.get(f"{API}/search", params={"track_name": clean, "artist_name": artist},
                         headers=HEADERS, timeout=10)
        results = r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        if exact:
            return exact
        raise
    results = _rank(results, title, artist, duration)
    best = results[0] if results else None
    if best and (best.get("syncedLyrics") or exact is None):
        return best
    return exact


# ---- your own lyrics (made in the editor), stored as standard .lrc files ----
USER_DIR = paths.MY_LYRICS_DIR
_TAG = re.compile(r"^\[(ti|ar|al|length|by|offset|re|ve):.*\]\s*$", re.I)


def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")[:100] or "_"


def user_file(title: str, artist: str) -> Path:
    return USER_DIR / f"{_safe(artist)} - {_safe(title)}.lrc"


def from_text(text: str) -> Lyrics | None:
    synced = parse_lrc(text)
    if synced:
        return Lyrics(synced, synced=True)
    plain = [l for l in text.splitlines() if l.strip() and not _TAG.match(l)]
    return Lyrics([(0.0, l) for l in plain], synced=False) if plain else None


def strip_tags(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not _TAG.match(l)).strip("\n")


def load_user_text(title: str, artist: str) -> str | None:
    f = user_file(title, artist)
    return strip_tags(f.read_text(encoding="utf-8")) if f.exists() else None


def _length_fits(f: Path, duration: float) -> bool:
    """False if your saved lyrics were made for a version of the song whose length is more
    than 5 s off the one playing (e.g. a radio edit with the same name). No length tag, or
    no length from the player: assume it fits."""
    try:
        m = re.search(r"^\[length:\s*(\d+):(\d+)", f.read_text(encoding="utf-8"), re.M | re.I)
    except OSError:
        return True
    if not m or not duration:
        return True
    return abs(int(m.group(1)) * 60 + int(m.group(2)) - duration) <= 5


def save_user(title: str, artist: str, text: str, album: str = "", duration: float = 0) -> Path:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    head = [f"[ti:{title}]", f"[ar:{artist}]"]
    if album:
        head.append(f"[al:{album}]")
    if duration:
        head.append(f"[length:{int(duration // 60):02d}:{int(duration % 60):02d}]")
    head.append("[by:autosynced-lyrics]")
    f = user_file(title, artist)
    f.write_text("\n".join(head) + "\n" + strip_tags(text) + "\n", encoding="utf-8")
    return f


def list_user() -> list[dict]:
    """Your saved lyrics: [{"file", "title", "artist", "lines", "synced", "modified"}], A-Z."""
    out = []
    for f in sorted(USER_DIR.glob("*.lrc")) if USER_DIR.exists() else []:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        tag = lambda k: next((m.group(1).strip() for m in re.finditer(rf"^\[{k}:(.*)\]\s*$", text, re.M | re.I)), "")
        artist, _, title = f.stem.partition(" - ")
        lyr = from_text(text)
        out.append({"file": f.name, "title": tag("ti") or title or f.stem, "artist": tag("ar") or artist,
                    "lines": len(lyr.lines) if lyr else 0, "synced": bool(lyr and lyr.synced),
                    "modified": time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime))})
    return sorted(out, key=lambda s: (s["title"].lower(), s["artist"].lower()))


def delete_user(title: str, artist: str) -> None:
    user_file(title, artist).unlink(missing_ok=True)


def search_online(title: str, artist: str, duration: float = 0, album: str = "") -> dict | None:
    """Best match for the editor: LRCLIB's exact match for this track's length (the same
    one the app shows), else the closest-length version by this artist, then other sites."""
    clean = _NOISE.sub("", title).strip() or title
    if duration:
        try:
            r = sources.http.get(f"{API}/get", params={"track_name": title, "artist_name": artist,
                                 "album_name": album, "duration": round(duration)},
                                 headers=HEADERS, timeout=10)
            exact = r.json() if r.ok else None
        except (requests.RequestException, ValueError):
            exact = None
        if exact and exact.get("syncedLyrics"):
            return {**_closer_version(exact, title, artist, duration), "source": "LRCLIB"}
    r = sources.http.get(f"{API}/search", params={"track_name": clean, "artist_name": artist},
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    results = _rank(r.json(), title, artist, duration)
    best = {**results[0], "source": "LRCLIB"} if results else None
    if best and best.get("syncedLyrics"):
        return best
    # No synced lyrics on LRCLIB: try the other synced source, then untimed fallbacks.
    found = sources.netease(title, artist, duration) or sources.qq_music(title, artist, duration)
    if not found:
        try:
            found = sources.kugou(title, artist, duration)
        except (requests.RequestException, ValueError, KeyError):
            found = None
    if found:
        return found
    if best:
        return best
    try:
        return sources.lyrics_ovh(title, artist)
    except (requests.RequestException, ValueError):
        return None


def fetch(title: str, artist: str, album: str = "", duration: float = 0,
          use_cache: bool = True) -> Lyrics | None:
    found, lyr = fetch_local(title, artist, duration, use_cache)
    return lyr if found else fetch_online(title, artist, album, duration, use_cache)


def fetch_local(title: str, artist: str, duration: float = 0, use_cache: bool = True):
    """(found, lyrics) from your own lyrics or the cache; instant, no network."""
    mine = load_user_text(title, artist)
    if mine is not None and _length_fits(user_file(title, artist), duration):
        return True, from_text(mine)
    if use_cache:
        hit, d = cache.get(title, artist, duration)
        if hit:
            return True, _from_cache(d)
    return False, None


def fetch_online(title: str, artist: str, album: str = "", duration: float = 0,
                 use_cache: bool = True) -> Lyrics | None:
    lyr = _from_record(_query(title, artist, album, duration) or {})   # errors aren't cached
    if lyr:
        lyr.online = True
    if use_cache:
        cache.put(title, artist, duration, _to_cache(lyr))
    return lyr


def remember_extras(title: str, artist: str, duration: float, lyr: Lyrics) -> None:
    """Add translations / word timing to the cached copy of online lyrics."""
    if lyr.online:
        cache.put(title, artist, duration, _to_cache(lyr), keep_age=True)


EXTRAS_VERSION = 3   # bump when translations / word timing are worked out differently:
                     # cached ones from before are then looked up again (lyrics are kept)


# ---- fixing lyrics timed for another version of the song ---------------------------
RETIME_LEAD = 0.15        # show a line this much before its first word is heard
RETIME_MIN_SHIFT = 0.3    # lines already this close are left alone
RETIME_MIN_POINTS = 3     # lines that must be measured before anything moves
RETIME_MAX_SHIFT = 15.0


def retime_offsets(times: list[float], heard: dict[int, float]) -> list[float] | None:
    """How far to move each line, from the lines whose real start was heard.

    Each measured line says "this part of the song is off by d seconds". Odd ones out
    (a repeated chorus line matched to the wrong repeat, a misheard word) are dropped by
    comparing with their neighbours, the rest are smoothed, and lines in between get a
    shift interpolated by time, so both a constant delay and a version with a longer
    or shorter section in the middle come out right. None if nothing needs moving."""
    pts = sorted((i, heard[i] - RETIME_LEAD - times[i]) for i in heard if 0 <= i < len(times))
    pts = [(i, d) for i, d in pts if abs(d) <= RETIME_MAX_SHIFT]
    if len(pts) < RETIME_MIN_POINTS:
        return None
    med = lambda xs: sorted(xs)[len(xs) // 2] if len(xs) % 2 else sum(sorted(xs)[len(xs) // 2 - 1:len(xs) // 2 + 1]) / 2
    keep = []
    for j, (i, d) in enumerate(pts):
        near = [pts[k][1] for k in range(max(0, j - 3), min(len(pts), j + 4))]
        if abs(d - med(near)) <= 0.8:
            keep.append((i, d))
    if len(keep) < RETIME_MIN_POINTS:
        return None
    smooth = [(i, med([keep[k][1] for k in range(max(0, j - 2), min(len(keep), j + 3))]))
              for j, (i, _) in enumerate(keep)]
    if max(abs(d) for _, d in smooth) < RETIME_MIN_SHIFT:
        return None
    out = []
    for n, t in enumerate(times):
        left = next(((i, d) for i, d in reversed(smooth) if i <= n), None)
        right = next(((i, d) for i, d in smooth if i >= n), None)
        if left is None:
            off = right[1]
        elif right is None or right[0] == left[0]:
            off = left[1]
        else:
            tl, tr = times[left[0]], times[right[0]]
            f = (t - tl) / (tr - tl) if tr > tl else 0.5
            off = left[1] + (right[1] - left[1]) * min(1.0, max(0.0, f))
        out.append(off)
    return out


def apply_retime(lyr: Lyrics) -> bool:
    """Move lyr's lines (and word timing that came with the file) to match when they
    were heard. True if anything changed."""
    if lyr.orig_times is None:
        lyr.orig_times = [t for t, _ in lyr.lines]
        lyr.orig_chars = [list(c) if c else None for c in lyr.char_times]
    offs = retime_offsets(lyr.orig_times, lyr.heard_starts)
    if offs is None:
        return False
    new, last = [], 0.0
    for t, off in zip(lyr.orig_times, offs):
        last = max(last, t + off, 0.0)
        new.append(round(last, 3))
    old = [t for t, _ in lyr.lines]
    if all(abs(a - b) < 0.05 for a, b in zip(new, old)):
        return False
    for i, (chars, off) in enumerate(zip(lyr.orig_chars, offs)):
        if chars and i not in lyr.heard_lines:     # timing from the file moves with its line
            lyr.char_times[i] = [(c[0] + off, c[1] + off) if c else None for c in chars]
    lyr.set_times(new)
    lyr.retimed = True
    return True


def _to_cache(lyr: Lyrics | None) -> dict | None:
    if lyr is None:
        return None
    r = lambda pair: [round(pair[0], 2), round(pair[1], 2)] if pair else None
    return {"v": EXTRAS_VERSION, "lines": [[round(t, 3), w] for t, w in lyr.lines], "synced": lyr.synced,
            # AI translations live in ai_translate's own store, not here (kept apart from human ones)
            "tr": [None if i in lyr.ai_lines else t for i, t in enumerate(lyr.translations)],
            "extras": lyr.extras_loaded, "extras_at": time.time(),
            "ct": [[r(c) for c in ct] if ct else None for ct in lyr.char_times],
            "rt": len(lyr.heard_starts) if lyr.retimed else 0}   # lines already fixed by listening


def _from_cache(d: dict) -> Lyrics | None:
    if d.get("lines") is None:
        return None                                  # remembered: nothing found online
    lyr = Lyrics([(t, w) for t, w in d["lines"]], d.get("synced", True))
    n = len(lyr.lines)
    if d.get("v") == EXTRAS_VERSION and len(d.get("tr") or []) == n and len(d.get("ct") or []) == n:
        lyr.translations = list(d["tr"])
        lyr.char_times = [[tuple(c) if c else None for c in ct] if ct else None for ct in d["ct"]]
        lyr.extras_loaded = bool(d.get("extras"))
    lyr.retime_done = (d.get("rt") or 0) >= 6     # enough lines were checked last time
    lyr.online = True
    return lyr
