"""Per-song timing corrections ("this song's lyrics are 0.5 s late"), set from the lyrics
bar's hover controls and remembered in song_offsets.json in the AppData folder."""
import json
import os

import paths

FILE = paths.DATA_DIR / "song_offsets.json"
STEP = 0.25            # seconds per click
_data: dict | None = None


def _key(title: str, artist: str) -> str:
    return f"{title.strip().lower()}\n{artist.strip().lower()}"


def _load() -> dict:
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _data = {}
    return _data


def get(title: str, artist: str) -> float:
    """Seconds to add to the song position for this song (+ = lyrics earlier)."""
    return float(_load().get(_key(title, artist), 0.0))


def nudge(title: str, artist: str, delta: float) -> float:
    """Change this song's correction by delta; returns the new value."""
    data = _load()
    key = _key(title, artist)
    value = round(data.get(key, 0.0) + delta, 2)
    if abs(value) < 1e-6:
        data.pop(key, None)
    else:
        data[key] = value
    try:
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, FILE)
    except OSError:
        pass
    return value
