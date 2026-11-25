"""Chinese translations for lyric lines no site has translated, written by Claude.

- Only lines without a human translation are sent. The whole song goes along as context
  (and the human translations around them, so the style matches).
- A song missing just a few lines -> Claude Haiku 5.5 (fractions of a cent).
  A song with no translation (or mostly none) -> Claude Sonnet 5.5, for 信达雅 quality.
- Every result is kept in ai_translations.json, so a line is translated only once.
- Every request is logged (tokens and cost) in ai_usage.json for the usage page.
- Uses the user's own API key (console.anthropic.com), stored encrypted for their Windows
  account only (Windows DPAPI) in api_key.bin.
"""
import ctypes
import ctypes.wintypes
import json
import os
import re
import threading
import time

import paths

KEY_FILE = paths.DATA_DIR / "api_key.bin"
USAGE_FILE = paths.DATA_DIR / "ai_usage.json"
# US$ per million tokens (input, output), Anthropic's list prices for these models.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}
MODEL_NAMES = {"claude-haiku-5-5": "Haiku 5.5", "claude-sonnet-5-5": "Sonnet 5.5"}
STORE_FILE = paths.DATA_DIR / "ai_translations.json"
FEW_MODEL = "claude-haiku-5-5"      # a few missing lines
FULL_MODEL = "claude-sonnet-5-5"    # a song with (almost) no translation
FULL_SHARE = 0.5                    # missing more than this share of lines -> FULL_MODEL

_lock = threading.Lock()
_store: dict | None = None


# ---- API key, encrypted with Windows DPAPI (readable only by this Windows user) ----
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError("Windows couldn't protect/unprotect the key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def get_key() -> str | None:
    try:
        return _dpapi(KEY_FILE.read_bytes(), protect=False).decode("utf-8") or None
    except (OSError, ValueError):
        return None


def set_key(key: str | None) -> None:
    key = (key or "").strip()
    if not key:
        KEY_FILE.unlink(missing_ok=True)
        return
    tmp = KEY_FILE.with_suffix(".tmp")
    tmp.write_bytes(_dpapi(key.encode("utf-8"), protect=True))
    os.replace(tmp, KEY_FILE)


def has_key() -> bool:
    return KEY_FILE.exists()


# ---- translations already made --------------------------------------------------
def _song(title: str, artist: str) -> str:
    return f"{title.strip().lower()}\n{artist.strip().lower()}"


def _load() -> dict:
    global _store
    if _store is None:
        try:
            _store = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _store = {}
    return _store


def known(title: str, artist: str) -> dict[str, str]:
    """{original line: AI translation} made earlier for this song."""
    with _lock:
        return {k: tidy(v) for k, v in _load().get(_song(title, artist), {}).items()}


def _remember(title: str, artist: str, pairs: dict[str, str]) -> None:
    with _lock:
        data = _load()
        data.setdefault(_song(title, artist), {}).update(pairs)
        tmp = STORE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, STORE_FILE)


# ---- usage & cost log --------------------------------------------------------------
def cost_of(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return tokens_in * p_in / 1e6 + tokens_out * p_out / 1e6


def usage_log() -> list[dict]:
    """Every AI translation request, oldest first:
    {"time", "title", "artist", "model", "lines", "in", "out", "cost"}."""
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _log_usage(entry: dict) -> None:
    with _lock:
        log = usage_log()
        log.append(entry)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(log, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, USAGE_FILE)


# ---- asking Claude -----------------------------------------------------------------
SYSTEM = """You translate song lyrics into Simplified Chinese for a karaoke-style lyrics display.

Translate in the spirit of 信达雅: faithful to the meaning (信), natural and fluent Chinese (达), and elegant, lyrical phrasing (雅) - the way a skilled human translator would subtitle a song, not a word-for-word gloss.
- Keep each translation to one line, about as long as the original line; it is read in a second or two while the song plays.
- Carry the song's mood, imagery and wordplay; idioms become natural Chinese equivalents.
- Keep names and brands as they are commonly written in Chinese; leave pure vocal sounds (oh, yeah, la la) as short sounds or leave them as they are.
- A word repeated as an echo or backing vocal ("dead, dead, dead") is translated once or as a natural phrase, never as one Chinese character over and over (not 死死死死).
- Where human translations of nearby lines are given, match their wording and tone.
- Do not censor or soften explicit language; translate it faithfully."""

_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "zh": {"type": "string"}},
        "required": ["n", "zh"], "additionalProperties": False}}},
    "required": ["lines"], "additionalProperties": False,
}


# Pure vocal sounds: nothing to translate. A line is skipped only if EVERY word in it is
# one of these (letters may be stretched: "ohhhh", "yeahhh"); one real word and it's sent.
_SYLLABLE = (r"o+h*|a+h*|e+h*|u+h*|h?m+|hm+|ye+a*h*|ye+h+|ya+h*|yo+|la+|na+|da+|do+|du+m+|"
             r"ba+|be+|bo+p+|bu+m+|bi+ng|ra+|ta+|sha+|shoo+|wo+p+|wo+|woo+|wee+|hoo+|ho+|ha+|"
             r"he+y*|ey+|ay+e*|whoa+|woah+|ooh+|huh|aha+|uh|sk+r+t+|br+|sh+|tra+|"
             # scat / drum sounds: "dum dum di-di-dum", "ding dong", "ba-dum-tss", "pa-pa-pam"
             r"di+|dee+|di+n+g*|du+n+|do+n+g|ba+m+|da+m+|pa+m*|pu+m+|ti+|tu+|tss+|"
             r"ri+|ru+|li+|lu+|ni+|nu+|bi+|bu+|ki+|ku+|zi+|zu+")
_SOUND = re.compile(rf"^(?:{_SYLLABLE})$")
# Sounds written joined together: "lalala", "hahaha", "nanana", "ohoh" (one syllable repeated).
# Not ma/pa, so "mama" / "papa" (real words) are still translated.
_REPEATED = re.compile(r"^((?:la|na|da|ha|he|ho|oh|ah|eh|uh|yeah|ya|yo|ra|ta|sha|doo|do|ba|wo|woo|hey|ay)h*)\1+$")
# Japanese / Korean / Chinese sung sounds (ラララ, ナナナ, 랄랄라, 啊啊, 啦啦)
_CJK_SOUND = re.compile(r"^[ラナアオウエイララーッらなあおうえいんンー라랄나날아오우에이야예啊哦喔呀啦嗯哈嘿呜噢\s、，,!！~～…·・-]+$")


def is_sounds(text: str) -> bool:
    """True if the line is only vocal sounds, like "ohh ohh", "yeah yeah", "la-la-la",
    "shoo-be-doo-wop" or "ラララ" - nothing to translate."""
    t = text.strip()
    if re.search(r"[^\x00-\x7f]", t):              # non-Latin script
        return bool(_CJK_SOUND.match(t)) and bool(re.search(r"\w", t))
    parts = [p for p in re.split(r"[^a-z]+", t.lower()) if p]
    return bool(parts) and all(_SOUND.match(p) or _REPEATED.match(p) for p in parts)


def has_kana(lines) -> bool:
    """Any Japanese kana in the song (then its kanji-only lines are Japanese, not Chinese)."""
    return any(re.search(r"[぀-ヿ]", t or "") for t in lines)


def is_echo(text: str) -> bool:
    """One word said 3+ times and nothing else, like "dead, dead, dead" or "(go go go)":
    a background echo, clear as it is (translated it reads like 死死死死)."""
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text.lower())
    return len(words) >= 3 and len(set(words)) == 1


# The same Chinese character 3+ times in a row (maybe with commas / spaces): kept once.
# Laughs and sung sounds (哈哈哈, 啦啦啦) are left alone.
_CHAR_RUN = re.compile(r"([一-鿿])(?:[\s,，、.。!！~～…-]*\1){2,}")
_SOUND_CHARS = set("哈啦呀哦啊嘿嗯呜喔噢耶嘻呵吧哒嘟咚当叮滴")


def tidy(zh: str) -> str:
    return _CHAR_RUN.sub(lambda m: m.group(0) if m.group(1) in _SOUND_CHARS else m.group(1), zh)


def worth_translating(text: str, japanese_song: bool = False) -> bool:
    """A real lyric line in any language but Chinese (not empty, a ♪, a credit, only
    sounds, or already Chinese). japanese_song: kanji-only lines count as Japanese."""
    t = text.strip()
    if not re.search(r"\w", t) or re.search(r"(作词|作曲|编曲|lyrics by|composed by)\s*[:：]", t, re.I):
        return False
    if is_sounds(t) or is_echo(t):
        return False
    han = len(re.findall(r"[一-鿿]", t))
    kana = re.search(r"[぀-ヿ]", t)
    if japanese_song:
        return True
    return not (han and not kana and han >= len(re.findall(r"\w", t)) * 0.5)


def choose_model(n_missing: int, n_lines: int) -> str:
    return FULL_MODEL if n_lines and n_missing / n_lines > FULL_SHARE else FEW_MODEL


def translate(title: str, artist: str, texts: list[str], human: list[str | None],
              want: list[int], client=None) -> dict[int, str]:
    """AI translations for lines `want` (indexes into texts). Uses and fills the store.
    Raises anthropic errors (no key, network, ...) for the caller to report."""
    done = known(title, artist)
    out = {i: done[texts[i]] for i in want if texts[i] in done}
    todo = [i for i in want if i not in out]
    if not todo:
        return out
    lyric = [i for i, t in enumerate(texts) if worth_translating(t)]
    model = choose_model(len(todo), len(lyric))

    import anthropic
    client = client or anthropic.Anthropic(api_key=get_key())
    song = "\n".join(
        f"{i}. {texts[i]}" + (f"    [human translation: {human[i]}]" if human[i] else "")
        for i in lyric)
    ask = (f"Song: {title} - {artist}\n\nLyrics (numbered):\n{song}\n\n"
           f"Translate these line numbers, which have no translation yet: "
           f"{', '.join(map(str, todo))}.\nReturn one entry per line number.")
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": ask}],
        output_config={"effort": "low" if model == FEW_MODEL else "medium",
                       "format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    used_in, used_out = response.usage.input_tokens, response.usage.output_tokens
    _log_usage({"time": time.strftime("%Y-%m-%d %H:%M"), "title": title, "artist": artist,
                "model": model, "lines": len(todo), "in": used_in, "out": used_out,
                "cost": round(cost_of(model, used_in, used_out), 6)})
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to translate this song")
    text = next((b.text for b in response.content if b.type == "text"), "")
    got = {}
    for item in json.loads(text).get("lines", []):
        n, zh = item.get("n"), (item.get("zh") or "").strip()
        if n in todo and zh:
            got[n] = tidy(zh)
    if got:
        _remember(title, artist, {texts[i]: zh for i, zh in got.items()})
    out.update(got)
    return out
