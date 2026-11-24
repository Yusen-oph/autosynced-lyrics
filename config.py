"""User settings, saved to settings.json in the user's AppData folder (see paths.py)."""
import json
import os
import sys
import winreg
from pathlib import Path

import paths

SETTINGS_FILE = paths.SETTINGS_FILE

DEFAULTS = {
    # app window style: "night" (dark, green accents) or "day" (light, rose accents)
    "ui_style": "night",
    "bar_follows_style": True,     # the lyrics bar uses the style's colours (not your own)
    # album cover in the app window, built from elements (presets set several at once)
    "cover_source": "album",       # album (the art), gradient (a colour mix), none (no cover)
    "cover_shape": "vinyl",        # square, rounded, circle, vinyl, cd, polaroid
    "cover_motion": "spin",        # still, spin, float, sway, pulse, tilt (follows the mouse)
    "cover_speed": 7.0,            # seconds per turn / per movement
    "cover_arm": True,             # tonearm on vinyl and CD
    "cover_glow": False,           # soft glow in the album's colour
    "cover_reflect": False,        # mirror reflection underneath
    # look
    "opacity": 1.0,                # whole overlay
    "bg_opacity": 0.55,            # background box only; 0 = text floats on the desktop
    "bg_color": "#111111",
    "font_family": "Segoe UI",
    "outline_enabled": True,
    "outline_color": "#000000",
    "outline_width": 2,
    "low_cpu": False,              # app window: no blur / reflection / spinning, slower redraws
    "soft_background": True,       # gentle gradient + soft shadow under the background
    "album_colors": True,          # current line / background coloured from the album cover
    # text
    "show_header": True,           # song title — artist on top
    "header_size": 11,
    "header_color": "#b3b3b3",
    "current_size": 24,
    "bold_current": True,
    "current_color": "#ffffff",
    "show_prev": True,
    "show_next": True,
    "other_size": 15,
    "other_color": "#8a8a8a",
    "show_progress": True,         # thin song-progress line along the bottom
    "show_translation": False,     # Chinese translation under each line (layout unchanged when off)
    "translation_size_pct": 60,
    "ai_translate": False,         # fill lines without a translation using Claude (your API key)    # translation size, % of its line
    # animation
    "animation": "slide",
    "animation_ms": 350,
    "bouncing_note": False,        # a note hopping along the words as they're sung
    "fix_timing_by_listening": None,   # move badly timed online lyrics to match the song (None = decide on first run)
    "learn_word_timing": None,     # learn word timing by listening (None = decide on first run)
    "note_offset": 0.0,            # s; + = the note hops earlier (e.g. for Bluetooth delay)
    # layout / timing / behaviour
    "width_pct": 60,
    "x": None,
    "y": None,
    "offset": 0.0,
    "hide_when_closed": True,
    "player": "auto",              # "auto" or a key of media_watcher.PLAYERS
    "lock_position": False,        # click-through, can't be dragged
    "hover_controls": True,        # play/pause, skip, settings, lock when the mouse is over the bar
    "check_updates": True,         # look for a new version on GitHub (at start, then every 6 h)
    "cache_lyrics": True,          # keep lyrics found online in a small cache (max 5 MB)
}

def load() -> dict:
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved.get("transparent_bg"):          # migrate from the first version
        saved.setdefault("bg_opacity", 0.0)
    if saved.get("spin_record") is False:    # the old "spinning record" switch, turned off
        saved.setdefault("cover_shape", "square")
        saved.setdefault("cover_motion", "still")
    settings.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return settings


def save(settings: dict) -> None:
    # write-then-rename so a crash mid-save can't corrupt the file
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


# ---- start with Windows (per-user Run key, no admin needed) ---------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "AutosyncedLyrics"


def launch_command() -> str:
    """How Windows starts the app at login: --tray = stay in the tray, no window."""
    exe = Path(sys.executable)
    if paths.FROZEN:                         # packaged: the .exe is the whole app
        return f'"{exe}" --tray'
    pythonw = exe.with_name("pythonw.exe")  # no console window
    return f'"{pythonw if pythonw.exists() else exe}" "{paths.CODE_DIR / "main.py"}" --tray'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_NAME)
            return True
    except OSError:
        return False


def refresh_startup() -> None:
    """Older versions registered the login command without --tray; update it once."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            current = winreg.QueryValueEx(key, _RUN_NAME)[0]
    except OSError:
        return
    if current != launch_command():
        set_startup(True)


def set_startup(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, _RUN_NAME)
            except OSError:
                pass
