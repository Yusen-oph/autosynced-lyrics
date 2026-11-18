"""Where the app keeps things.

App files (code, bundled libraries) live wherever the app was unzipped or installed.
Personal data lives in the Windows user's own AppData folder, separately for every
Windows account on every computer, and is never part of a shared build:

    %APPDATA%\\AutosyncedLyrics\\
        settings.json     overlay look, position, options
        my_lyrics\\        lyrics you synced yourself (.lrc)
        models\\           Whisper speech models (downloaded on first auto-sync)
        lyrics_cache.db   lyrics found online, max 5 MB (see cache.py; can be cleared)
        song_offsets.json per-song timing fixes from the lyrics bar's - / + buttons
        ai_translations.json  Chinese lines translated by Claude (each line only once)
        ai_usage.json     each AI translation request: date, song, model, tokens, cost
        api_key.bin       your Claude API key, encrypted for your Windows account (if set)
        icon2.ico

If accounts are added later, each account can get its own subfolder here.
"""
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "AutosyncedLyrics"
FROZEN = getattr(sys, "frozen", False)          # True inside the packaged .exe
CODE_DIR = Path(__file__).resolve().parent       # source folder (or the bundle's folder)

DATA_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
SETTINGS_FILE = DATA_DIR / "settings.json"
MY_LYRICS_DIR = DATA_DIR / "my_lyrics"
MODELS_DIR = DATA_DIR / "models"
ICON_FILE = DATA_DIR / "icon2.ico"   # (icon.ico was the first, plainer icon)


def prepare() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not FROZEN:
        _migrate_from(CODE_DIR)


def _migrate_from(old: Path) -> None:
    """One-time move of data that earlier versions kept next to the code."""
    old_settings = old / "settings.json"
    if old_settings.exists() and not SETTINGS_FILE.exists():
        shutil.move(str(old_settings), SETTINGS_FILE)
    old_lyrics = old / "my_lyrics"
    if old_lyrics.is_dir():
        MY_LYRICS_DIR.mkdir(exist_ok=True)
        for f in old_lyrics.glob("*.lrc"):
            if not (MY_LYRICS_DIR / f.name).exists():
                shutil.move(str(f), MY_LYRICS_DIR / f.name)
        if not any(old_lyrics.iterdir()):
            old_lyrics.rmdir()
    old_models = old / "models"
    if old_models.is_dir() and not MODELS_DIR.exists():
        shutil.move(str(old_models), MODELS_DIR)   # ~600 MB: move, don't re-download
