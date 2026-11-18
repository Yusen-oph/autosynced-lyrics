"""Build a shareable Windows app:  .venv\\Scripts\\python build.py

Produces  dist\\Autosynced Lyrics\\Autosynced Lyrics.exe  and  dist\\AutosyncedLyrics-Windows.zip.
Nothing personal goes in: settings, your synced lyrics and the Whisper models live in
each user's own %APPDATA%\\AutosyncedLyrics folder, created on first run.
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Autosynced Lyrics"
DIST, WORK = ROOT / "dist", ROOT / "build"
APP_DIR = DIST / NAME
ZIP = DIST / "AutosyncedLyrics-Windows.zip"
UPDATE_ZIP = DIST / "AutosyncedLyrics-Update.zip"

FRIEND_README = """Autosynced Lyrics
=================

Shows synced lyrics over your desktop for whatever Spotify, Apple Music,
NetEase Cloud Music or QQ Music is playing.

How to start
  1. Unzip this whole folder somewhere (e.g. Downloads). Keep the files together.
  2. Double-click "Autosynced Lyrics.exe".
  3. Windows may say "Windows protected your PC" because this hobby app isn't signed:
     click "More info" -> "Run anyway".
  4. It asks whether to install. Yes puts it in the Start menu and Windows search (pin it
     to the taskbar from there), adds a desktop shortcut, and lists it in Settings -> Apps
     so it can be uninstalled. After that you can delete the unzipped folder.
  5. Play a song in your music app. The lyrics bar appears near the bottom of the screen.

Using it
  - Opening the app shows its window: Now Playing (big synced lyrics), My Lyrics,
    Settings and AI Usage, in a Night (Spotify-style) or Day (Apple Music-style) look.
  - Closing the window keeps the app in the system tray (icons by the clock) with the
    lyrics bar on screen. Quit it from the tray icon.
  - Right-click the lyrics bar: Settings, Edit / sync lyrics, Hide, Quit.
  - Drag the bar to move it.
  - Songs without synced lyrics online: right-click -> Edit / sync lyrics. Tap-sync them
    yourself, or let AI auto-sync listen to the song (downloads a speech model the first time).
  - Settings -> Text: show a Chinese translation under each line.
  - Settings -> Animation: a jelly note that hops along the words as they're sung.

Your data
  Settings, lyrics you sync and a small lyrics cache (max 5 MB, can be cleared in
  Settings -> Behavior) are stored on your own computer in
  %APPDATA%\\AutosyncedLyrics  (paste that into File Explorer's address bar to open it).
  Lyrics are copyrighted: keep your synced files for personal use.

Requirements: Windows 10 or 11, and the Spotify, Apple Music, NetEase Cloud Music or
QQ Music desktop app.
"""


def main():
    sys.path.insert(0, str(ROOT))
    import tray

    WORK.mkdir(exist_ok=True)
    icon = WORK / "icon.ico"
    tray.make_icon_image(256).save(icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                (128, 128), (256, 256)])

    cmd = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "main.py"),
        "--name", NAME, "--windowed", "--onedir", "--noconfirm", "--clean",
        "--icon", str(icon),
        "--distpath", str(DIST), "--workpath", str(WORK / "pyinstaller"), "--specpath", str(WORK),
        # libraries PyInstaller can't fully discover on its own
        "--collect-all", "faster_whisper",
        "--collect-all", "ctranslate2",
        "--collect-binaries", "pyaudiowpatch",
        "--collect-submodules", "winrt",
        "--collect-data", "sv_ttk",          # Windows 11 theme files (.tcl, images)
        "--hidden-import", "pystray._win32",
        "--hidden-import", "selfcheck",
        "--hidden-import", "anthropic",       # loaded only when AI translation runs
        # the app window: its page, pywebview and the .NET bridge it uses for WebView2
        "--add-data", f"{ROOT / 'ui'};ui",
        "--collect-all", "webview",
        "--collect-all", "pythonnet",
        "--collect-all", "clr_loader",
        "--hidden-import", "clr",
        # big things this app never uses
        "--exclude-module", "torch", "--exclude-module", "matplotlib",
        "--exclude-module", "pandas", "--exclude-module", "IPython",
    ]
    print("Running PyInstaller…")
    subprocess.run(cmd, check=True, cwd=ROOT)

    (APP_DIR / "README.txt").write_text(FRIEND_README, encoding="utf-8")

    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(APP_DIR.rglob("*")):
            z.write(f, Path(NAME) / f.relative_to(APP_DIR))
        names = z.namelist()

    def personal(name: str) -> bool:
        parts = name.replace("\\", "/").split("/")
        return (parts[-1] in ("settings.json", "model.bin") or parts[-1].endswith(".lrc")
                or "my_lyrics" in parts or parts[1:2] == ["models"])
    leaked = [n for n in names if personal(n)]
    if leaked:
        ZIP.unlink()
        sys.exit(f"Refusing to ship personal files: {leaked[:5]}")

    size = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file())
    print(f"App folder: {APP_DIR}  ({size / 1e6:.0f} MB)")
    print(f"Zip:        {ZIP}  ({ZIP.stat().st_size / 1e6:.0f} MB, {len(names)} files)")

    # The small update: just the .exe (all the app's code) and the window's pages.
    import installer
    import updater
    n = updater.make_update_zip(APP_DIR, UPDATE_ZIP, installer.VERSION)
    print(f"Update zip: {UPDATE_ZIP}  ({UPDATE_ZIP.stat().st_size / 1e6:.0f} MB, {n} files)"
          f"  - Settings -> About -> Update from file, for people who already have the app")


if __name__ == "__main__":
    main()
