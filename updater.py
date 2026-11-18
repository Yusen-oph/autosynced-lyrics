"""Small updates: replace just the app's own code instead of downloading everything again.

The packaged app is ~200 MB, but nearly all of that is Python and its libraries in
_internal\\, which rarely change. The app's own code is inside the .exe, and the window's
pages are in _internal\\ui. So an update zip (made by build.py) holds only:

    update.json              {"app", "version", "base"}
    Autosynced Lyrics.exe
    _internal/ui/...

"base" is a fingerprint of the rest of _internal (names and sizes). If the update was
built on a different base (e.g. a new library was added), it can't be applied: the full
download is needed that one time.
"""
import hashlib
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import installer
import paths

MANIFEST = "update.json"
UI_PREFIX = "_internal/ui/"
MAX_SIZE = 150_000_000


def fingerprint(app_dir: Path) -> str:
    """Names + sizes of everything in _internal except the ui pages."""
    internal = app_dir / "_internal"
    h = hashlib.sha256()
    for f in sorted(internal.rglob("*")):
        rel = f.relative_to(internal).as_posix()
        if not f.is_file() or rel.split("/")[0] in ("ui", "ui.old"):
            continue
        h.update(f"{rel}:{f.stat().st_size}\n".encode("utf-8"))
    return h.hexdigest()[:24]


def make_update_zip(app_dir: Path, out: Path, version: str) -> int:
    """Used by build.py. Returns the number of files in the zip."""
    manifest = {"app": installer.APP_NAME, "version": version, "base": fingerprint(app_dir)}
    files = [app_dir / installer.EXE_NAME] + sorted(f for f in (app_dir / "_internal" / "ui").rglob("*")
                                                   if f.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=1))
        for f in files:
            z.write(f, f.relative_to(app_dir).as_posix())
    return len(files) + 1


def _safe_name(name: str) -> bool:
    if name == installer.EXE_NAME or name == MANIFEST:
        return True
    if not name.startswith(UI_PREFIX) or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    return all(p not in (".", "..") for p in parts) and all(parts[2:-1])


def apply(data: bytes) -> str:
    """Install an update zip over the running app. Returns the new version.
    Raises ValueError with a message for the user if it can't be applied."""
    if not paths.FROZEN:
        raise ValueError("Updates only work in the packaged app (this is running from the source code).")
    if len(data) > MAX_SIZE:
        raise ValueError("That file is too big to be an update.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(z.read(MANIFEST))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise ValueError("That isn't an Autosynced Lyrics update file (AutosyncedLyrics-Update.zip).")
    names = z.namelist()
    if manifest.get("app") != installer.APP_NAME or installer.EXE_NAME not in names:
        raise ValueError("That isn't an Autosynced Lyrics update file.")
    bad = [n for n in names if not _safe_name(n)]
    if bad:
        raise ValueError(f"The update file has unexpected contents ({bad[0]}).")

    app_dir = installer.running_exe().parent
    if manifest.get("base") != fingerprint(app_dir):
        raise ValueError("This update adds new parts to the app, so it needs the full download "
                         "(AutosyncedLyrics-Windows.zip) this time.")

    staging = app_dir / "_update_tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        for n in names:
            if n != MANIFEST and not n.endswith("/"):
                target = staging / n
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(n))

        exe, old_exe = app_dir / installer.EXE_NAME, app_dir / (installer.EXE_NAME + ".old")
        ui, old_ui = app_dir / "_internal" / "ui", app_dir / "_internal" / "ui.old"
        old_exe.unlink(missing_ok=True)
        shutil.rmtree(old_ui, ignore_errors=True)
        exe.rename(old_exe)                 # a running .exe can be renamed, not overwritten
        try:
            shutil.move(str(staging / installer.EXE_NAME), str(exe))
            if (staging / "_internal" / "ui").exists():
                if ui.exists():
                    ui.rename(old_ui)
                shutil.move(str(staging / "_internal" / "ui"), str(ui))
        except Exception:
            exe.unlink(missing_ok=True)     # put the old version back
            old_exe.rename(exe)
            if old_ui.exists() and not ui.exists():
                old_ui.rename(ui)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    version = str(manifest.get("version") or "")
    if installer.is_installed_copy():
        try:
            installer.VERSION = version or installer.VERSION
            installer._register(installer.installed_exe())   # Settings -> Apps shows the new version
        except OSError:
            pass
    return version


def restart() -> None:
    """Start the new version a moment after this one has quit."""
    exe = installer.running_exe().parent / installer.EXE_NAME
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & start "" "{exe}"',
                     creationflags=installer.NO_WINDOW, cwd=str(exe.parent))


def cleanup() -> None:
    """Remove what the last update left behind (the old .exe can't be deleted while it runs)."""
    if not paths.FROZEN:
        return
    app_dir = installer.running_exe().parent
    try:
        (app_dir / (installer.EXE_NAME + ".old")).unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(app_dir / "_internal" / "ui.old", ignore_errors=True)
    shutil.rmtree(app_dir / "_update_tmp", ignore_errors=True)


# ---- updates from GitHub ---------------------------------------------------------
# Each release on GitHub has three files: update.json (tiny, says which base the update
# needs), AutosyncedLyrics-Update.zip (the small update) and AutosyncedLyrics-Windows.zip
# (everything, for new installs or when the base changed).
REPO = "Yusen-oph/autosynced-lyrics"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_API = f"https://api.github.com/repos/{REPO}/releases/latest"
_HEADERS = {"User-Agent": "autosynced-lyrics-updater", "Accept": "application/vnd.github+json"}
UPDATE_ASSET, FULL_ASSET = "AutosyncedLyrics-Update.zip", "AutosyncedLyrics-Windows.zip"


def _ver(text: str) -> tuple:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def newer(version: str, than: str) -> bool:
    return _ver(version) > _ver(than)


def check() -> dict | None:
    """The latest release if it's newer than this app, else None. Raises requests errors
    (no internet, GitHub blocked) for the caller to ignore or show."""
    import requests
    r = requests.get(_API, headers=_HEADERS, timeout=10)
    if r.status_code == 404:                 # no releases yet
        return None
    r.raise_for_status()
    rel = r.json()
    version = (rel.get("tag_name") or "").lstrip("vV")
    if not version or not newer(version, installer.VERSION):
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets", [])}
    small_ok = False
    if paths.FROZEN and assets.get(MANIFEST) and assets.get(UPDATE_ASSET):
        try:
            m = requests.get(assets[MANIFEST], headers=_HEADERS, timeout=10).json()
            small_ok = m.get("base") == fingerprint(installer.running_exe().parent)
        except (requests.RequestException, ValueError):
            small_ok = False
    return {"version": version, "notes": (rel.get("body") or "")[:2000],
            "page": rel.get("html_url") or RELEASES_PAGE,
            "update_url": assets.get(UPDATE_ASSET), "full_url": assets.get(FULL_ASSET),
            "small_ok": small_ok}


def download_and_apply(info: dict, progress=lambda pct: None) -> str:
    """Download the small update for `info` (from check()) and install it."""
    import requests
    if not info.get("small_ok") or not info.get("update_url"):
        raise ValueError("This update needs the full download.")
    url = info["update_url"]
    if not url.startswith("https://github.com/" + REPO + "/"):
        raise ValueError("Unexpected download address.")
    with requests.get(url, headers=_HEADERS, timeout=30, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        if total > MAX_SIZE:
            raise ValueError("The update is too big.")
        buf = bytearray()
        for chunk in r.iter_content(256 * 1024):
            buf += chunk
            if len(buf) > MAX_SIZE:
                raise ValueError("The update is too big.")
            if total:
                progress(round(len(buf) * 100 / total))
    return apply(bytes(buf))
