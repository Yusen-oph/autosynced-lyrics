"""Installs the packaged app like normal Windows software (no admin rights needed):

- copies the app folder to %LOCALAPPDATA%\\Programs\\Autosynced Lyrics
- Start menu shortcut (so it shows up in Windows search and can be pinned to the taskbar)
- desktop shortcut
- an entry in Settings -> Apps -> Installed apps, whose Uninstall runs `--uninstall`

Your data (settings, synced lyrics, cache, models) lives in %APPDATA%\\AutosyncedLyrics and
is never touched by installing; uninstalling asks before deleting it.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import winreg
from pathlib import Path

import paths

APP_NAME = "Autosynced Lyrics"
EXE_NAME = f"{APP_NAME}.exe"
VERSION = "2.13"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / APP_NAME
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AutosyncedLyrics"
DECLINED = paths.DATA_DIR / "install_declined"
IPC_PORT = 47811
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def running_exe() -> Path:
    return Path(sys.executable).resolve()


def is_installed_copy() -> bool:
    """True when this is the installed app (not the zip / dist folder / source code)."""
    return paths.FROZEN and running_exe().parent == INSTALL_DIR.resolve()


def installed_exe() -> Path:
    return INSTALL_DIR / EXE_NAME


def should_offer() -> bool:
    return paths.FROZEN and not is_installed_copy() and not DECLINED.exists()


def _tell_running_app(msg: bytes) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2) as c:
            c.sendall(msg)
        return True
    except OSError:
        return False


def _wait_until_closed(seconds: float = 8.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=0.3):
                pass
        except OSError:
            return
        time.sleep(0.3)


_FOLDERS = (r"$(if ($env:AL_DIRS) { $env:AL_DIRS -split '\|' } "
            "else { @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop')) })")


def _shortcuts(exe: Path, folders: list[str] | None = None) -> None:
    """Start menu + desktop shortcuts (Windows Script Host via PowerShell; paths go in
    environment variables, so no quoting problems). `folders` overrides where (tests)."""
    script = (
        "$w = New-Object -ComObject WScript.Shell;"
        f"foreach ($dir in {_FOLDERS}) {{"
        "  $s = $w.CreateShortcut((Join-Path $dir ($env:AL_NAME + '.lnk')));"
        "  $s.TargetPath = $env:AL_EXE; $s.WorkingDirectory = $env:AL_DIR;"
        "  $s.IconLocation = $env:AL_EXE + ',0'; $s.Description = 'Synced lyrics over your desktop';"
        "  $s.Save() }")
    env = dict(os.environ, AL_EXE=str(exe), AL_DIR=str(exe.parent), AL_NAME=APP_NAME,
               AL_DIRS="|".join(folders or []))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=env, check=True, creationflags=NO_WINDOW, timeout=60)


def _remove_shortcuts(folders: list[str] | None = None) -> None:
    script = (f"foreach ($dir in {_FOLDERS}) {{"
              "  $p = Join-Path $dir ($env:AL_NAME + '.lnk'); if (Test-Path $p) { Remove-Item $p } }")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=dict(os.environ, AL_NAME=APP_NAME, AL_DIRS="|".join(folders or [])),
                   creationflags=NO_WINDOW, timeout=60)


def _register(exe: Path) -> None:
    size_kb = sum(f.stat().st_size for f in INSTALL_DIR.rglob("*") if f.is_file()) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", VERSION),
                            ("Publisher", APP_NAME), ("DisplayIcon", f'"{exe}",0'),
                            ("InstallLocation", str(INSTALL_DIR)),
                            ("UninstallString", f'"{exe}" --uninstall')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name, value in (("EstimatedSize", size_kb), ("NoModify", 1), ("NoRepair", 1)):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)


def install(progress=lambda msg: None) -> Path:
    """Copy this app into INSTALL_DIR and add shortcuts + the Installed apps entry."""
    src = running_exe().parent
    progress("Closing the running app…")
    if _tell_running_app(b"quit"):
        _wait_until_closed()
    progress("Copying files…")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, INSTALL_DIR, dirs_exist_ok=True)
    exe = installed_exe()
    progress("Adding shortcuts…")
    _shortcuts(exe)
    _register(exe)
    return exe


def uninstall(delete_data: bool) -> None:
    _tell_running_app(b"quit")
    _wait_until_closed()
    import config
    config.set_startup(False)
    _remove_shortcuts()
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    if delete_data:
        shutil.rmtree(paths.DATA_DIR, ignore_errors=True)
    # The program folder can only go once this process has exited.
    subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /s /q "{INSTALL_DIR}"',
                     creationflags=NO_WINDOW, cwd=str(INSTALL_DIR.parent))


# ---- the little dialogs (Tk) ---------------------------------------------------------
def offer_install() -> bool:
    """Ask whether to install. Returns True if the installed copy was started (so this one
    should quit)."""
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    answer = messagebox.askyesnocancel(
        APP_NAME,
        "Install Autosynced Lyrics on this PC?\n\n"
        "It will appear in the Start menu and Windows search, get a desktop shortcut, "
        "can be pinned to the taskbar, and can be uninstalled from Settings → Apps.\n\n"
        "Yes: install now\nNo: just run it this time\nCancel: don't ask again", parent=root)
    if answer is None:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        DECLINED.write_text("declined")
    if not answer:
        root.destroy()
        return False
    try:
        exe = install()
    except Exception as exc:
        messagebox.showerror(APP_NAME, f"Installing didn't work:\n{exc}\n\nThe app will just run from here.",
                             parent=root)
        root.destroy()
        return False
    root.destroy()
    subprocess.Popen([str(exe)], cwd=str(exe.parent))
    return True


def run_uninstall() -> int:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno(APP_NAME, "Uninstall Autosynced Lyrics?", parent=root):
        return 0
    delete = messagebox.askyesno(
        APP_NAME, "Also delete your settings, the lyrics you synced, the lyrics cache and the "
                  f"speech models?\n\n({paths.DATA_DIR})\n\nNo keeps them, in case you install it again.",
        default="no", parent=root)
    uninstall(delete)
    messagebox.showinfo(APP_NAME, "Autosynced Lyrics was uninstalled.", parent=root)
    root.destroy()
    return 0
