"""System-tray icon. Menu clicks are passed to the Tk thread through a queue."""
import threading
from pathlib import Path

import pystray
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import paths

ICON_FILE = paths.ICON_FILE


def make_icon_image(size: int = 64) -> Image.Image:
    """The app icon: a vinyl record with lyric lines on a rose-to-violet tile.
    Drawn 4x larger (max 1024) and scaled down, so small sizes stay smooth."""
    S = min(1024, max(256, size * 4))
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    t = (xx * 0.35 + yy * 0.65) / S
    top, bottom = np.array([255, 64, 110], np.float32), np.array([112, 56, 235], np.float32)
    rgb = (top + (bottom - top) * t[..., None]).astype(np.uint8)
    img = Image.fromarray(np.dstack([rgb, np.full((S, S), 255, np.uint8)]), "RGBA")
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((S * .1, S * .02, S * .9, S * .6), fill=(255, 255, 255, 60))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * .08)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=round(S * 0.23), fill=255)
    img.putalpha(mask)

    cx, cy, R = S * .6, S * .5, S * .33                    # the record
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((cx - R, cy - R + S * .02, cx + R, cy + R + S * .02), fill=(30, 0, 50, 140))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(S * .025)))
    d = ImageDraw.Draw(img)
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(22, 22, 28, 255))
    for k in range(9):                                       # grooves
        r = R * (0.95 - k * 0.06)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(52, 52, 62, 255), width=max(1, round(S * .004)))
    shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shine)
    sd.pieslice((cx - R, cy - R, cx + R, cy + R), 200, 245, fill=(255, 255, 255, 38))
    sd.pieslice((cx - R, cy - R, cx + R, cy + R), 20, 65, fill=(255, 255, 255, 22))
    img.alpha_composite(shine)
    d = ImageDraw.Draw(img)
    r = R * .36
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 255))     # label
    r = R * .07
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(112, 56, 235, 255))       # spindle hole
    x0 = S * .11                                             # lyric lines
    for i, (w, a) in enumerate(((.25, 255), (.17, 240), (.22, 225))):
        y, h = S * (.355 + i * .115), S * .062
        d.rounded_rectangle((x0, y, x0 + S * w, y + h), radius=h / 2, fill=(255, 255, 255, a))
    return img.resize((size, size), Image.LANCZOS) if size != S else img


def ensure_ico() -> Path:
    if not ICON_FILE.exists():
        ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
        img = make_icon_image(256)
        img.save(ICON_FILE, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON_FILE


def start(events) -> pystray.Icon:
    menu = pystray.Menu(
        pystray.MenuItem("Open Autosynced Lyrics", lambda: events.put("window"), default=True),
        pystray.MenuItem("Settings…", lambda: events.put("settings")),
        pystray.MenuItem("Edit / sync lyrics…", lambda: events.put("editor")),
        pystray.MenuItem("Show / hide lyrics", lambda: events.put("toggle")),
        pystray.MenuItem("Lock / unlock position", lambda: events.put("unlock")),
        pystray.MenuItem("Quit", lambda: events.put("quit")),
    )
    icon = pystray.Icon("autosynced-lyrics", make_icon_image(), "Autosynced Lyrics", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon
