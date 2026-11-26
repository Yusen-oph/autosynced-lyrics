"""Wallpaper lyrics: big lyrics on the desktop, over your wallpaper (also a live one like
Wallpaper Engine's) and under every app window, like QQ Music's desktop wallpaper lyrics.

It's a see-through, click-through window owned by the desktop window (Progman): Windows
keeps a window owned by the desktop just above it, under all apps, and still shows it after
Win+D. (Putting it *inside* the wallpaper layer doesn't show above Wallpaper Engine on
Windows 11 24H2.) It sits in front of the desktop icons, but clicks pass through to them.

Cheap: it's redrawn only when the line changes (plus a short fade), never per frame; while
app windows cover it Windows simply doesn't show it.
"""
import ctypes
import threading
import time
import tkinter as tk
from ctypes import wintypes

from PIL import Image

import render
from layered import LayeredWindow, system_scale

user32 = ctypes.windll.user32
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]

GWLP_HWNDPARENT = -8                 # for a top-level window: its owner
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
HWND_BOTTOM = wintypes.HWND(1)
WS_EX_LAYERED = 0x00080000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010

POSITIONS = {"Left": "left", "Middle": "center", "Right": "right"}
FADE_S = 0.3
OWNER_CHECK_S = 5.0                  # Explorer restarted: a new desktop window to sit on
AI_MARK = "⁣"                   # same mark as overlay.AI_MARK: an AI translation


def _work_area() -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
        return r.left, r.top, r.right, r.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


class WallpaperLyrics:
    def __init__(self, master, settings: dict):
        self.s = settings
        self.scale = system_scale()
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.geometry("1x1+0+0")
        self.layer = LayeredWindow(self.win)
        self.layer.set_click_through(True)
        hwnd = self.layer.hwnd
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
        # Hidden and shown through Windows, never Tk's withdraw / deiconify: after those,
        # Windows refuses per-pixel drawing on the window (UpdateLayeredWindow: error 87).
        user32.ShowWindow(hwnd, SW_HIDE)
        self.shown = False
        self.owner = None
        self._owner_checked = 0.0
        self.key = None              # what's wanted: (song, line, settings version, ...)
        self.drawn_key = None        # what the last drawing was for
        self.drawing = False         # a drawing thread is running
        self.ready = None            # (image, x, y) drawn, waiting to be shown
        self.fade = None             # start time of the fade-in
        self.colors = None
        self._version = 0
        self._args = None

    # ---- settings ----------------------------------------------------------
    def apply(self):
        self._version += 1

    def set_colors(self, colors: dict | None):
        if colors != self.colors:
            self.colors = colors
            self._version += 1

    def _px(self, pt) -> int:
        return max(6, round(pt * 96 / 72 * self.scale))

    def _styles(self):
        s = self.s
        cur_color = self.colors["cur"] if (self.colors and s["album_colors"]) else "#ffffff"
        big = self._px(s["wall_size"])
        cur = render.TextStyle(s["font_family"], True, big, cur_color, False, "#000000", 0)
        other = render.TextStyle(s["font_family"], False, max(8, round(big * 0.62)), "#ffffff",
                                 False, "#000000", 0)
        return cur, other

    # ---- showing -----------------------------------------------------------
    def _sit_on_desktop(self):
        """Owned by the desktop window, at the bottom: over the wallpaper, under every app."""
        now = time.monotonic()
        if now - self._owner_checked < OWNER_CHECK_S and self.owner:
            return
        self._owner_checked = now
        progman = user32.FindWindowW("Progman", None)
        if progman and progman != self.owner:
            self.owner = progman
            user32.SetWindowLongPtrW(self.layer.hwnd, GWLP_HWNDPARENT, progman)
            user32.SetWindowPos(self.layer.hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def hide(self):
        if self.shown:
            user32.ShowWindow(self.layer.hwnd, SW_HIDE)
            self.shown = False
        self.key = self.drawn_key = self.ready = self.fade = None

    def busy(self) -> bool:
        """Drawing or fading: the app ticks fast meanwhile, to show it as soon as it's ready."""
        return self.fade is not None or self.drawing or self.ready is not None

    def update(self, song, lines: list[tuple[str, str]], idx: int, header: str):
        """lines: (text, translation) for the whole song; idx: the line being sung (-1 = not
        yet). Call every tick: it only draws when something changed, and then off the app's
        thread (a new frame takes ~50 ms), so the lyrics bar never stutters."""
        if not self.s["wall_lyrics"] or not lines:
            return self.hide()
        self._sit_on_desktop()
        self.key = (song, idx, self._version, id(lines), sum(1 for _, t in lines if t))
        self._args = (lines, idx, header)
        if self.key != self.drawn_key and not self.drawing:
            self._start_drawing()
        if self.ready is not None:
            img, x, y, data = self.ready
            self.ready = None
            fade = self.shown and self.s["animation"] != "none"
            self._show(img, x, y, 0.35 if fade else 1.0, data)
            self.fade = time.monotonic() if fade else None
        elif self.fade is not None:
            e = render.ease((time.monotonic() - self.fade) / FADE_S)
            if e >= 1:
                self.fade = None
            self.layer.set_opacity(self.s["wall_opacity"] * (0.35 + 0.65 * min(1.0, e)))

    def _start_drawing(self):
        key, (lines, idx, header) = self.key, self._args
        self.drawn_key, self.drawing = key, True

        def work():
            try:
                img, x, y = self._draw(lines, idx, header)
                frame = (img, x, y, img.tobytes("raw", "BGRa"))   # ready for the screen
            except Exception:
                frame = None
            if self.key == key and frame is not None:   # still wanted (not hidden / moved on)
                self.ready = frame
            self.drawing = False
        threading.Thread(target=work, daemon=True).start()

    def _show(self, img, x, y, fade_from: float = 1.0, data: bytes = None):
        opacity = self.s["wall_opacity"] * fade_from
        if not self.layer.update(img, x, y, opacity, data):
            # Windows refused the picture: switching the layered style off and on resets it.
            ex = user32.GetWindowLongPtrW(self.layer.hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(self.layer.hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
            user32.SetWindowLongPtrW(self.layer.hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
            self.layer.update(img, x, y, opacity, data)
        if not self.shown:                      # picture first, then show: no empty flash
            user32.ShowWindow(self.layer.hwnd, SW_SHOWNOACTIVATE)
            self.shown = True
            self.owner = None                   # set the owner again, at the bottom
            self._sit_on_desktop()

    # ---- drawing -----------------------------------------------------------
    def _draw(self, lines, idx, header):
        """The lines around idx in a fixed-size, see-through canvas: the line being sung at
        the middle, the ones before above it, the ones after below, fading away from it."""
        s = self.s
        left, top, right, bottom = _work_area()
        sw, sh = right - left, bottom - top
        pos = s["wall_position"]
        w = round(sw * (0.6 if pos == "center" else 0.42))
        h = round(sh * 0.8)
        margin = round(sw * 0.05)
        x = {"left": left + margin, "right": right - margin - w}.get(pos, left + (sw - w) // 2)
        y = top + (sh - h) // 2
        cur_st, other_st = self._styles()
        pad = round(cur_st.px * 0.5)              # room for the soft shadow
        max_w = w - 2 * pad
        tr_pct = s["translation_size_pct"] / 100
        tr_on = s["show_translation"]
        gap = round(cur_st.px * 0.55)

        def block(i):
            text, tr = lines[i]
            st = cur_st if i == idx else other_st
            img = render.text_image(text or "♪", st, max_w)
            if tr_on and tr:
                tst = render.TextStyle(st.family, False, max(6, round(st.px * tr_pct)), st.color,
                                       False, "#000000", 0)
                img = _stack(img, render.text_image(tr.lstrip(AI_MARK), tst, max_w),
                             round(render.TR_GAP * self.scale), pos)
            return img

        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        mid = h // 2
        place = []                                # (image, top y, alpha)
        c = max(0, idx)
        cur_img = block(c)
        cy = mid - cur_img.height // 2
        place.append((cur_img, cy, 1.0 if idx >= 0 else 0.55))
        n = max(0, (s["wall_lines"] - 1) // 2)
        yy = cy                                   # lines before, upwards
        for k in range(1, n + 1):
            if c - k < 0:
                break
            img = block(c - k)
            yy -= gap + img.height
            place.append((img, yy, max(0.15, 0.6 - 0.18 * (k - 1))))
        yy = cy + cur_img.height                  # lines after, downwards
        for k in range(1, s["wall_lines"] - n):
            if c + k >= len(lines):
                break
            img = block(c + k)
            yy += gap
            place.append((img, yy, max(0.15, 0.6 - 0.18 * (k - 1))))
            yy += img.height
        if s["show_header"] and header:
            hst = render.TextStyle(cur_st.family, False, max(8, round(cur_st.px * 0.42)), "#ffffff",
                                   False, "#000000", 0)
            himg = render.text_image(header, hst, max_w)
            top_y = min(p[1] for p in place)
            place.append((himg, max(pad, top_y - gap * 2 - himg.height), 0.7))
        for img, ty, alpha in place:
            if ty + img.height < 0 or ty > h:
                continue
            if pos == "left":
                lx = pad
            elif pos == "right":
                lx = w - pad - img.width
            else:
                lx = (w - img.width) // 2
            it = render.Item(img, lx + img.width / 2, ty + img.height / 2, alpha)
            render.draw_items(canvas, [it])
        # A soft dark shadow behind the words, so they read on any wallpaper.
        canvas = render.with_glow(canvas, max(4, round(cur_st.px * 0.35)), 150)
        box = canvas.getbbox()                    # just the words: less to copy to the screen
        if box:
            canvas = canvas.crop(box)
            x, y = x + box[0], y + box[1]
        return canvas, x, y


def _stack(top, bottom, gap: int, align: str):
    """A line with its translation under it, both aligned to the same side."""
    w = max(top.width, bottom.width)
    out = Image.new("RGBA", (w, top.height + gap + bottom.height), (0, 0, 0, 0))
    for img, y in ((top, 0), (bottom, top.height + gap)):
        x = {"left": 0, "right": w - img.width}.get(align, (w - img.width) // 2)
        out.alpha_composite(img, (x, y))
    return out
