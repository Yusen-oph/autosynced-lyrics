"""The main app window: a separate process showing ui/ (HTML/CSS) in a native window
(pywebview, using Windows' built-in Edge WebView2).

The running app (main.py) starts it with `--window <url> <style>`; the page talks back to
the app through app_server.py. Closing the window leaves the app (tray, lyrics bar) running.
"""
import ctypes
import subprocess
import sys
from pathlib import Path

import paths

TITLE = "Autosynced Lyrics"
BACKGROUND = {"night": "#121212", "day": "#f5f5f7"}


def launch(url: str, style: str) -> subprocess.Popen:
    if paths.FROZEN:
        cmd = [sys.executable, "--window", url, style]
    else:
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        cmd = [str(pythonw if pythonw.exists() else exe), str(paths.CODE_DIR / "main.py"),
               "--window", url, style]
    return subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def is_open() -> bool:
    """An app window is already on screen (maybe started by an earlier run of the app)."""
    return bool(ctypes.windll.user32.FindWindowW(None, TITLE))


def bring_to_front() -> None:
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def _dress_window(style: str) -> None:
    """Title bar to match the style (dark for Night, light for Day) and the app's icon."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    dark = ctypes.c_int(1 if style == "night" else 0)
    for attr in (20, 19):                    # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old Windows 10)
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark), 4) == 0:
            break
    ico = paths.CODE_DIR / "ui" / "app.ico"              # shipped with the app
    if ico.exists():
        for size, which in ((16, 0), (32, 1)):            # ICON_SMALL, ICON_BIG
            icon = user32.LoadImageW(None, str(ico), 1, size, size, 0x10)
            if icon:
                user32.SendMessageW(hwnd, 0x0080, which, icon)   # WM_SETICON
    # Make Windows redraw the frame so the new title bar colour shows at once.
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)      # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED


def _watch_front(window) -> None:
    """Tell the page whether anyone is looking: the window in front and not minimised.
    WebView2 keeps rendering at full speed behind other windows or minimised, so the page
    can't tell by itself; when it's in the background the page pauses its animations and
    redraws only a few times a second (see setQuiet in ui/app.js)."""
    import threading
    import time
    if getattr(window, "_watching_front", False):
        return                              # once per window, not per page load
    window._watching_front = True
    user32 = ctypes.windll.user32

    def loop():
        last = None
        while True:
            time.sleep(1.0)
            hwnd = user32.FindWindowW(None, TITLE)
            if not hwnd:
                continue
            quiet = bool(user32.IsIconic(hwnd)) or user32.GetForegroundWindow() != hwnd
            if quiet != last:
                try:
                    window.evaluate_js(f"window.setQuiet && setQuiet({'true' if quiet else 'false'})")
                    last = quiet
                except Exception:
                    pass
    threading.Thread(target=loop, daemon=True).start()


class _Api:
    """Called from the page (window.pywebview.api)."""
    _window = None          # private: pywebview doesn't hand "_" names to the page

    def set_style(self, style):
        _dress_window("day" if style == "day" else "night")

    def set_fullscreen(self, on):
        """Fill the whole screen (no title bar or taskbar) or go back to a normal window."""
        w = self._window
        if w is None:
            return False
        # pywebview never updates window.fullscreen after toggle_fullscreen(), so ask the
        # native window itself; trusting the stale flag made Exit a no-op and the next
        # "full screen" toggle back to a normal window (title bar showing).
        if not on:
            # Frame drawing back on BEFORE the title bar returns: with it off, Windows
            # draws the old classic title bar for a moment.
            _square_edges(False)
        if bool(on) != _is_full(w):
            w.toggle_fullscreen()
            w.fullscreen = bool(on)
        if on:
            _square_edges(True)
            import threading
            threading.Timer(0.3, _square_edges, (True,)).start()   # again once Windows settles
        return _is_full(w)


def _square_edges(full: bool) -> None:
    """Full screen: no rounded corners and no 1-px window outline; back to normal when leaving.
    pywebview's window shadow (on by default) pulls the Windows frame 1 px into the window,
    and that frame -- a light outline with Windows 11's rounded corners -- stays drawn even
    without a title bar. So in full screen the frame comes out and Windows stops drawing it."""
    hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    dwm = ctypes.windll.dwmapi

    class _Margins(ctypes.Structure):
        _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]
    m = 0 if full else 1                                     # pywebview's own value is 1
    dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(_Margins(m, m, m, m)))
    policy = ctypes.c_int(1 if full else 2)                  # DWMNCRP_DISABLED / ENABLED (pywebview: 2)
    dwm.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(policy), 4)   # DWMWA_NCRENDERING_POLICY
    corner = ctypes.c_int(1 if full else 0)                  # DWMWCP_DONOTROUND / DEFAULT
    border = ctypes.c_uint(0xFFFFFFFE if full else 0xFFFFFFFF)   # DWMWA_COLOR_NONE / DEFAULT
    dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)  # DWMWA_WINDOW_CORNER_PREFERENCE
    dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)  # DWMWA_BORDER_COLOR


def _is_full(w) -> bool:
    try:
        from webview.platforms.winforms import BrowserView
        form = BrowserView.instances.get(w.uid)
        if form is not None:
            return bool(form.is_fullscreen)
    except Exception:
        pass
    return bool(getattr(w, "fullscreen", False))


def main(argv: list[str]) -> int:
    try:
        i = argv.index("--window")
        url, style = argv[i + 1], (argv[i + 2] if len(argv) > i + 2 else "night")
    except (ValueError, IndexError):
        return 2
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    try:
        import webview
    except Exception:
        return 3                            # app falls back to the classic settings window
    api = _Api()
    window = webview.create_window(TITLE, url, width=1200, height=780, min_size=(940, 620),
                                   background_color=BACKGROUND.get(style, "#121212"),
                                   text_select=False, js_api=api)
    api._window = window
    window.events.shown += lambda: (_dress_window(style), None)[1]
    window.events.loaded += lambda: (_watch_front(window), None)[1]
    try:
        webview.start(gui="edgechromium", private_mode=True)
    except Exception:
        return 4
    return 0
