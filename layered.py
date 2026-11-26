"""Per-pixel-alpha window drawing via Win32 UpdateLayeredWindow (smooth edges over the desktop)."""
import ctypes
from ctypes import wintypes

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
ULW_ALPHA = 0x2
AC_SRC_OVER, AC_SRC_ALPHA = 0x0, 0x1
DIB_RGB_COLORS, BI_RGB = 0, 0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def system_scale() -> float:
    try:
        return user32.GetDpiForSystem() / 96
    except Exception:
        return 1.0


class LayeredWindow:
    def __init__(self, tk_toplevel):
        tk_toplevel.update_idletasks()
        self.hwnd = int(tk_toplevel.wm_frame(), 16)  # the real top-level HWND, not Tk's child
        self.click_through = False
        self.ensure_style()

    def ensure_style(self):
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        want = ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        want = (want | WS_EX_TRANSPARENT) if self.click_through else (want & ~WS_EX_TRANSPARENT)
        if want != ex:
            user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, want)

    def set_click_through(self, on: bool):
        self.click_through = on
        self.ensure_style()

    _mem = None        # memory DC + DIB reused between frames of the same size
    _bmp = _old = _bits = None
    _size = None

    def _ensure_buffer(self, w: int, h: int):
        if self._size == (w, h):
            return
        self._free_buffer()
        screen = user32.GetDC(None)
        self._mem = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h  # top-down
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bmi.bmiHeader.biCompression = BI_RGB
        self._bits = ctypes.c_void_p()
        self._bmp = gdi32.CreateDIBSection(self._mem, ctypes.byref(bmi), DIB_RGB_COLORS,
                                           ctypes.byref(self._bits), None, 0)
        self._old = gdi32.SelectObject(self._mem, self._bmp)
        self._size = (w, h)

    def _free_buffer(self):
        if self._mem:
            gdi32.SelectObject(self._mem, self._old)
            gdi32.DeleteObject(self._bmp)
            gdi32.DeleteDC(self._mem)
        self._mem = self._bmp = self._old = self._bits = self._size = None

    def set_opacity(self, opacity: float) -> bool:
        """Change only the whole window's opacity (no new picture): nearly free."""
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, round(opacity * 255))), AC_SRC_ALPHA)
        return bool(user32.UpdateLayeredWindow(self.hwnd, None, None, None, None, None, 0,
                                               ctypes.byref(blend), ULW_ALPHA))

    def update(self, img: Image.Image, x: int, y: int, opacity: float = 1.0, data: bytes = None) -> bool:
        """data: img already turned into premultiplied BGRA (e.g. on another thread)."""
        w, h = img.size
        self._ensure_buffer(w, h)
        if data is None:
            data = img.tobytes("raw", "BGRa")  # premultiplied BGRA in one pass, as Windows wants
        ctypes.memmove(self._bits, data, len(data))
        screen = user32.GetDC(None)
        try:
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, round(opacity * 255))), AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(
                self.hwnd, screen, ctypes.byref(wintypes.POINT(x, y)), ctypes.byref(wintypes.SIZE(w, h)),
                self._mem, ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
            return bool(ok)
        finally:
            user32.ReleaseDC(None, screen)
