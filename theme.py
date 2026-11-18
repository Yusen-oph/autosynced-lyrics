"""Windows 11 look for the Settings and editor windows (Sun Valley theme), light or dark
following Windows' own app mode."""
import ctypes
import winreg

try:
    import sv_ttk
except ImportError:          # theme missing: plain Tk look, everything still works
    sv_ttk = None


def windows_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


DARK = windows_dark()
MUTED = "#9d9d9d" if DARK else "#666666"          # secondary text
# Lyrics text box in the editor (a classic Tk widget the theme doesn't reach)
TEXT = {"bg": "#1f1f1f", "fg": "#e8e8e8", "insert": "#ffffff", "select": "#264f78"} if DARK else \
       {"bg": "#ffffff", "fg": "#1a1a1a", "insert": "#000000", "select": "#cce4f7"}
TAGS = {   # editor highlights: current line, synced, guessed timing, unsure word
    "target": {"background": "#5a4a14" if DARK else "#ffe58a"},
    "done": {"foreground": "#5fd47f" if DARK else "#1a7f37"},
    "guess": {"background": "#5c3d1e" if DARK else "#ffd8b0"},
    "unsure": {"background": "#6e2626" if DARK else "#ffb3b3", "underline": True},
}


def apply(root) -> None:
    if sv_ttk is not None:
        try:
            sv_ttk.set_theme("dark" if DARK else "light", root)
            # The theme sets its colours on a "theme changed" event, which never reaches
            # the app's hidden main window: run that step directly.
            root.tk.call("configure_colors")
        except Exception:
            pass


def title_bar(win) -> None:
    """Dark title bar on a window when Windows is in dark mode."""
    if not DARK:
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        on = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, older Windows 10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
    except Exception:
        pass
