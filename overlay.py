"""The floating, always-on-top lyrics bar: header, outlined lines, animated line changes,
optional Chinese translations under each line and a note that hops along the words."""
import ctypes
import ctypes.wintypes
import math
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import render
from layered import LayeredWindow, system_scale

ANIMATIONS = {  # label shown in settings -> internal name
    "None": "none",
    "Fade": "fade",
    "Slide up": "slide",
    "Turn over (flip)": "flip",
    "Spin": "spin",
    "Zoom": "zoom",
    "Swing": "swing",
}


@dataclass(frozen=True)
class View:
    header: str = ""
    prev: str = ""
    cur: str = ""
    nxt: str = ""
    idx: int | None = None        # lyric line index, None for status messages
    song: tuple | None = None
    prev_tr: str = ""             # Chinese translations (used only when tr_on)
    cur_tr: str = ""
    nxt_tr: str = ""
    tr_on: bool = False           # translation rows shown for this song

    def tr(self, role: str) -> str:
        return {"prev": self.prev_tr, "cur": self.cur_tr, "next": self.nxt_tr}.get(role, "") \
            if self.tr_on else ""


NOTES = "♪♫♬♩"
AI_MARK = "\u2063"   # leads a translation written by AI (shown with a small "AI" tag)
NOTE_ENTER, NOTE_EXIT = 0.35, 0.45   # seconds to fall in / fly away
NOTE_FALL = 0.22                     # falling part of the entrance; then it lands and wobbles
NOTE_CROUCH = 0.08                   # squashing down before a jump
NOTE_LEAD = 0.07                     # run the note this much ahead: its landing squish
                                     # peaks ~60 ms after touchdown, plus a frame of display


def _jelly(t: float) -> float:
    """Squash after a landing: a big squish that wobbles out like jelly (damped spring).
    Starts from 0, so the squish builds over a few frames instead of snapping."""
    if t < 0 or t > 0.9:
        return 0.0
    return 0.5 * math.exp(-6.0 * t) * math.sin(2 * math.pi * 4.2 * t)


# Lyrics-bar colours for each app window style (used when "bar_follows_style" is on).
STYLE_COLORS = {
    "night": {"bg_color": "#121212", "current_color": "#ffffff", "other_color": "#a7a7a7",
              "header_color": "#1ed760", "outline_color": "#000000"},
    "day": {"bg_color": "#f5f5f7", "current_color": "#1d1d1f", "other_color": "#6e6e73",
            "header_color": "#fa2d48", "outline_color": "#ffffff"},
}


def _hex_rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a: str, b: str, e: float) -> str:
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * e):02x}" for x, y in zip(ra, rb))


# Hover controls: (name, glyph in Windows' icon font); None = a divider
CONTROLS = [("prev", "\uE892"), ("play", "\uE768"), ("next", "\uE893"), None,
            ("later", "\uE738"), ("earlier", "\uE710"), None,     # this song's timing: - / +
            ("settings", "\uE713"), ("lock", "\uE72E")]
PAUSE_GLYPH = "\uE769"
UNLOCK_GLYPH = "\uE785"
HOVER_FADE = 0.15        # seconds to fade the controls in / out
HOVER_LINGER = 2.0       # keep them this long after the mouse leaves, then fade out


def _icon_font(px: int):
    fonts_dir = Path(r"C:\Windows\Fonts")
    for name in ("SegoeIcons.ttf", "segmdl2.ttf"):     # Windows 11, Windows 10
        if (fonts_dir / name).exists():
            return ImageFont.truetype(str(fonts_dir / name), px)
    return None


class Overlay:
    def __init__(self, master, settings: dict, on_moved, on_menu, on_control=None):
        self.s = settings
        self.on_moved = on_moved
        self.on_menu = on_menu
        self.on_control = on_control or (lambda name: None)
        self.colors = None            # {"cur", "bg"} from the album cover, or None
        self.progress = None          # (position getter, duration) for the progress line
        self.playing = True
        self.hover = 0.0              # controls' visibility 0..1
        self._hover_seen = -10.0      # last time the mouse was over the bar
        self._hover_t = time.monotonic()
        self._hover_hot = None        # button under the mouse
        self._pressed = None          # button being clicked (instead of dragging)
        self._prog_px = None
        self._box = (1, 1)
        self._pills = {}
        self.scale = system_scale()
        self.visible = True
        self.view = View()
        self.anim = None              # (old_view, new_view, start_time, kind)
        self._last_size = None
        self.note = None              # (song, line index, word start times, singing end,
                                      #  real: True = real word timing, False = estimated)
        self.note_pos = lambda: 0.0   # current song position, set by the app
        self._version = 0             # bumps on settings change (invalidates cached frame)
        self._base_key = self._base = None

        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry("1x1+0+0")
        self.layer = LayeredWindow(self.win)
        self.win.bind("<ButtonPress-1>", self._press)
        self.win.bind("<B1-Motion>", self._drag)
        self.win.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Button-3>", lambda e: self.on_menu(e.x_root, e.y_root))
        self.apply()

    # ---- settings ---------------------------------------------------------
    def _px(self, pt) -> int:
        return max(4, round(pt * 96 / 72 * self.scale))

    def _c(self, key: str) -> str:
        """A colour setting, or the app style's colour when the bar follows the style."""
        if self.s.get("bar_follows_style"):
            return STYLE_COLORS.get(self.s.get("ui_style"), {}).get(key, self.s[key])
        return self.s[key]

    def _style(self, which: str) -> render.TextStyle:
        s = self.s
        cur_color = self.colors["cur"] if self.colors else self._c("current_color")
        size, color, bold = {
            "header": (s["header_size"], self._c("header_color"), False),
            "cur": (s["current_size"], cur_color, s["bold_current"]),
            "other": (s["other_size"], self._c("other_color"), False),
        }[which]
        return render.TextStyle(s["font_family"], bold, self._px(size), color, s["outline_enabled"],
                                self._c("outline_color"), max(1, round(s["outline_width"] * self.scale)))

    def _tr_style(self, which: str, ai: bool = False) -> render.TextStyle:
        """Translation under a line: smaller, same colour, not bold."""
        b = self._style(which)
        px = max(6, round(b.px * self.s["translation_size_pct"] / 100))
        return render.TextStyle(b.family, False, px, b.color, b.outline, b.outline_color,
                                max(1, b.outline_px - 1))

    def _role_style(self, role: str) -> str:
        return {"header": "header", "cur": "cur"}.get(role, "other")

    def _block(self, text: str, tr: str, which: str):
        """A lyric line, with its translation under it when there is one."""
        img = render.text_image(text, self._style(which), self._max_w)
        if not tr:
            return img
        ai = tr.startswith(AI_MARK)
        tr = tr.lstrip(AI_MARK)
        st = self._tr_style(which)
        timg = render.text_image(tr, st, self._max_w)
        if ai:
            timg = self._with_ai_tag(timg, st, st.color)
        return render.stack(img, timg, round(render.TR_GAP * self.scale))

    def _with_ai_tag(self, img, st, color):
        """A translation with a small "AI" tag after it, so it's clear Claude wrote it."""
        tag = render.badge("AI", max(10, round(st.px * 0.72)), color, st.outline_color,
                           round(self.scale))
        return render.hstack(img, tag, round(5 * self.scale))

    def _note_space(self) -> int:
        """Room above the current line for the bouncing note (only when it's switched on)."""
        if not self.s["bouncing_note"]:
            return 0
        return round(self._style("cur").px * 0.45)

    def apply(self):
        """Re-read settings and redraw."""
        s = self.s
        self._version += 1
        self.screen_w = self.win.winfo_screenwidth()
        self.width = int(self.screen_w * s["width_pct"] / 100)
        if s["x"] is None or s["y"] is None:
            s["x"] = (self.screen_w - self.width) // 2
            s["y"] = int(self.win.winfo_screenheight() * 0.72)
        self.layer.set_click_through(s["lock_position"])
        self.redraw()

    # ---- content ----------------------------------------------------------
    def set_view(self, view: View):
        if view == self.view:
            return
        old, self.view = self.view, view
        kind = self.s["animation"]
        if kind == "none" or not self.visible:
            self.anim = None
        elif old.song == view.song and old.idx is not None and view.idx is not None:
            self.anim = (old, view, time.monotonic(), kind if view.idx == old.idx + 1 else "fade")
        elif old.song != view.song or old.cur != view.cur:
            self.anim = (old, view, time.monotonic(), "fade")
        self.redraw()

    def set_visible(self, visible: bool):
        if visible == self.visible:
            return
        self.visible = visible
        if visible:
            self.win.deiconify()
            self.layer.ensure_style()
            self._last_size = None
            self.redraw()
        else:
            self.win.withdraw()

    def set_note(self, note, pos_getter):
        self.note, self.note_pos = note, pos_getter

    def _note_time(self) -> float:
        """Song time the note follows: a little ahead, plus the user's own fine-tuning."""
        return self.note_pos() + NOTE_LEAD + self.s["note_offset"]

    def set_colors(self, colors: dict | None):
        """Album-cover colours ({"cur", "bg"}), or None for the user's own."""
        if colors != self.colors:
            self.colors = colors
            self._version += 1
            self.redraw()

    def set_progress(self, pos_getter, duration: float, playing: bool = True):
        self.progress = (pos_getter, duration) if pos_getter and duration > 0 else None
        if playing != self.playing:
            self.playing = playing
            if self.hover > 0:
                self.redraw()

    def busy(self) -> bool:
        """True while something is moving (needs ~60 fps redraws)."""
        return (self.anim is not None or self._note_window() or self._hover_moving()
                or self._flash is not None)

    def _note_window(self) -> bool:
        if not (self.visible and self.s["bouncing_note"] and self.note):
            return False
        song, idx, starts, end = self.note[:4]
        if (song, idx) != (self.view.song, self.view.idx) or not starts:
            return False
        p = self._note_time()
        return starts[0] - 0.05 <= p <= end + NOTE_CROUCH + NOTE_EXIT

    def tick(self):
        hover_changed = self._update_hover()
        if self.busy() or hover_changed:
            self.redraw()
        elif self._note_was_on:
            self.redraw()          # one last frame without the note
        elif self._progress_px() != self._prog_px:
            self.redraw()          # the progress line moved a pixel
        self._note_was_on = self._note_window()

    _note_was_on = False

    # ---- layout & drawing -------------------------------------------------
    def _layout(self, view: View) -> tuple[dict, int]:
        """slot name -> (image or None, center_y); plus total height."""
        s, pad, gap = self.s, round(14 * self.scale), round(6 * self.scale)
        max_w = self._max_w = self.width - 2 * pad
        rows = []
        if s["show_header"] and view.header:
            rows.append(("header", view.header, self._style("header")))
        if s["show_prev"]:
            rows.append(("prev", view.prev, self._style("other")))
        rows.append(("cur", view.cur, self._style("cur")))
        if s["show_next"]:
            rows.append(("next", view.nxt, self._style("other")))

        slots, y = {}, pad
        for name, text, style in rows:
            which = self._role_style(name)
            if name == "cur":
                y += self._note_space()
            if view.tr_on and name != "header":
                img = self._block(text, view.tr(name), which)
                blank = (render.blank_height(style) + round(render.TR_GAP * self.scale)
                         + render.blank_height(self._tr_style(which)))
                h = max(img.height if img else 0, blank)   # same height with or without a translation
            else:
                img = render.text_image(text, style, max_w)
                h = img.height if img else render.blank_height(style)  # keep layout steady
            slots[name] = (img, y + h / 2)
            y += h + (gap * 2 if name == "header" else gap)
        return slots, y - gap + pad

    def _items(self, slots, alpha=1.0, dy=0.0, skip=()):
        cx = self.width / 2
        return [render.Item(img, cx, cy + dy, alpha) for name, (img, cy) in slots.items()
                if img is not None and name not in skip]

    def _frame_items(self):
        new_slots, new_h = self._layout(self.view)
        self._slots = new_slots
        if not self.anim:
            return self._items(new_slots), new_h
        old_view, _, start, kind = self.anim
        t = (time.monotonic() - start) * 1000 / max(1, self.s["animation_ms"])
        if t >= 1:
            self.anim = None
            return self._items(new_slots), new_h
        e = render.ease(t)
        old_slots, old_h = self._layout(old_view)
        h = max(new_h, old_h)
        cx = self.width / 2

        items = []
        # The header only animates when the song changes.
        if old_view.header == self.view.header and "header" in new_slots:
            items += self._items({"header": new_slots["header"]})
            skip = ("header",)
        else:
            skip = ()

        if kind == "slide":
            items += self._scroll(old_slots, new_slots, e, old_view)
        elif kind in ("flip", "spin", "zoom", "swing"):
            items += self._items(old_slots, 1 - e, 0, skip + ("cur",))
            items += self._items(new_slots, e, 0, skip + ("cur",))
            o_img, o_cy = old_slots["cur"]
            n_img, n_cy = new_slots["cur"]
            if kind in ("flip", "spin"):
                f = abs(math.cos(math.pi * e))
                img, cy = (o_img, o_cy) if e < 0.5 else (n_img, n_cy)
                if img is not None:
                    items.append(render.Item(img, cx, cy, 1.0, sx=f if kind == "spin" else 1,
                                             sy=f if kind == "flip" else 1))
            elif kind == "zoom":
                if o_img is not None:
                    items.append(render.Item(o_img, cx, o_cy, 1 - e, 1 + 0.35 * e, 1 + 0.35 * e))
                if n_img is not None:
                    items.append(render.Item(n_img, cx, n_cy, e, 0.6 + 0.4 * e, 0.6 + 0.4 * e))
            else:  # swing
                if o_img is not None:
                    items.append(render.Item(o_img, cx, o_cy + e * 20 * self.scale, 1 - e, angle=-12 * e))
                if n_img is not None:
                    items.append(render.Item(n_img, cx, n_cy - (1 - e) * 20 * self.scale, e,
                                             angle=12 * (1 - e)))
        else:  # fade
            items += self._items(old_slots, 1 - e, 0, skip)
            items += self._items(new_slots, e, 0, skip)
        return items, h

    def _scroll(self, old, new, e, old_view: View):
        """Every line moves up one row; each one morphs into the size/colour of its new row."""
        cx = self.width / 2
        old_texts = {"prev": old_view.prev, "cur": old_view.cur, "next": old_view.nxt}
        tr_gap = round(render.TR_GAP * self.scale)
        roles = [r for r in ("prev", "cur", "next") if r in new]
        if len(roles) >= 2:
            step = new[roles[1]][1] - new[roles[0]][1]
        else:
            img = new["cur"][0] or old["cur"][0]
            step = (img.height if img else 40) + 6 * self.scale
        lerp = lambda a, b: a + (b - a) * e
        items = []

        for i, role in enumerate(roles):
            o_img, o_cy = old[role]
            if i == 0:                       # top line leaves upwards
                if o_img is not None:
                    items.append(render.Item(o_img, cx, o_cy - e * step, max(0.0, 1 - 2 * e)))
                continue
            n_img, n_cy = new[roles[i - 1]]  # the row this line moves into
            cy = lerp(o_cy, n_cy)
            text = old_texts[role]
            if not text:
                continue
            # One copy of the line in its destination shape, smoothly scaled from its old
            # size and recoloured: no re-rasterising per frame, so no size stepping.
            wa, wb = self._role_style(role), self._role_style(roles[i - 1])
            a, b = self._style(wa), self._style(wb)
            color = _mix(a.color, b.color, e)
            masks = render.text_masks(text, b.family, b.bold, b.px,
                                      b.outline_px if b.outline else 0, self._max_w)
            img = render.colorize(masks, color, b.outline_color)
            tr = old_view.tr(role)
            if tr:
                ai = tr.startswith(AI_MARK)
                tr = tr.lstrip(AI_MARK)
                tb = self._tr_style(wb)
                tmasks = render.text_masks(tr, tb.family, False, tb.px,
                                           tb.outline_px if tb.outline else 0, self._max_w)
                timg = render.colorize(tmasks, color, tb.outline_color)
                if ai:
                    timg = self._with_ai_tag(timg, tb, color)
                img = render.stack(img, timg, tr_gap)
            k = lerp(a.px / b.px, 1.0)
            items.append(render.Item(img, cx, cy, 1.0, k, k))

        n_img, n_cy = new[roles[-1]]         # new bottom line comes in from below
        if n_img is not None:
            items.append(render.Item(n_img, cx, n_cy + (1 - e) * step, e))
        return items

    def _shadow_on(self) -> bool:
        return self.s["soft_background"] and self.s["bg_opacity"] >= 0.08

    def _controls_on(self) -> bool:
        """Controls can show: always when unlocked; when locked, while Ctrl is held."""
        return self.s["hover_controls"] and (not self.s["lock_position"] or self._peek)

    _peek = False   # locked bar made clickable for a moment (Ctrl held over it)

    def _glow_on(self) -> bool:
        """Text-only mode (no background): a soft dark glow behind the words instead."""
        return self.s["soft_background"] and self.s["bg_opacity"] < 0.08

    def _margins(self) -> tuple[int, int, int]:
        """Transparent room around the box: for the shadow, and above/below for the controls.
        Fully transparent pixels let clicks through, so this room doesn't get in the way."""
        mx = round(18 * self.scale) if self._shadow_on() else 0
        zone = self._pill_size()[1] + round(8 * self.scale) if self.s["hover_controls"] else 0
        return mx, max(mx, zone), max(mx, zone)

    def redraw(self):
        if not self.visible:
            return
        s = self.s
        mx, mt, mb = margins = self._margins()
        key = (self.view, self._version, margins)
        if self.anim is None and self._base_key == key:
            canvas = self._base.copy()         # lines unchanged: reuse, only the extras move
        else:
            items, h = self._frame_items()
            w, h = self.width, int(h)
            bg_alpha = round(s["bg_opacity"] * 255)
            if not s["lock_position"]:
                bg_alpha = max(bg_alpha, 1)  # invisible but still grabbable for dragging
            bg = (*_hex_rgb(self.colors["bg"] if self.colors else self._c("bg_color")), bg_alpha)
            radius = round(14 * self.scale)
            box = render.compose_on(render.box_background((w, h), bg, radius, s["soft_background"]),
                                    items)
            if self._glow_on():
                box = render.with_glow(box, round(5 * self.scale), 150)
            if margins == (0, 0, 0):
                canvas = box
            else:
                strength = round(120 * bg_alpha / 255) if self._shadow_on() else 0
                canvas = render.drop_shadow((w, h), radius, margins, strength,
                                            round(3 * self.scale)).copy()
                canvas.alpha_composite(box, (mx, mt))
            self._box = (w, h)
            if self.anim is None:
                self._base_key, self._base = key, canvas.copy()
        self._draw_progress(canvas, mx, mt)
        note = self._note_item()
        if note is not None:
            note.cx += mx
            note.cy += mt
            render.draw_items(canvas, [note])
        self._draw_controls(canvas)
        self.layer.update(canvas, int(s["x"]) - mx, int(s["y"]) - mt, s["opacity"])

    # ---- progress line ------------------------------------------------------
    def _progress_geom(self):
        w, h = self._box
        inset = round(16 * self.scale)
        return inset, w - inset, h - round(6 * self.scale), max(2, round(2 * self.scale))

    def _progress_px(self):
        if not (self.s["show_progress"] and self.progress and self.visible):
            return None
        get, dur = self.progress
        x0, x1, _, _ = self._progress_geom()
        return round(max(0.0, min(1.0, get() / dur)) * (x1 - x0))

    def _draw_progress(self, canvas, mx, mt):
        px = self._prog_px = self._progress_px()
        if px is None:
            return
        x0, x1, y, t = self._progress_geom()
        # Drawn on its own strip and blended in: drawing straight onto the canvas would
        # replace the background's pixels and punch a see-through line into it.
        strip = Image.new("RGBA", (x1 - x0 + 1, t + 1), (0, 0, 0, 0))
        d = ImageDraw.Draw(strip)
        color = _hex_rgb(self._style("cur").color)
        d.rounded_rectangle((0, 0, x1 - x0, t), t, fill=(255, 255, 255, 38))
        if px > 0:
            d.rounded_rectangle((0, 0, max(px, t), t), t, fill=(*color, 220))
        canvas.alpha_composite(strip, (mx + x0, mt + y - t))

    # ---- hover controls -----------------------------------------------------
    def _pill_size(self) -> tuple[int, int]:
        bs, pad, sep = round(30 * self.scale), round(4 * self.scale), round(10 * self.scale)
        n = sum(1 for c in CONTROLS if c)
        dividers = sum(1 for c in CONTROLS if c is None)
        return pad * 2 + n * bs + dividers * sep, bs + pad * 2

    def _pill_colors(self) -> dict:
        """Hover controls in the app window's style: Night (dark, green) or Day (frosted, rose)."""
        if self.s.get("ui_style") == "day":
            return {"bg": (250, 250, 252, 240), "edge": (0, 0, 0, 30), "icon": (29, 29, 31, 235),
                    "sep": (0, 0, 0, 40), "hot": (0, 0, 0, 22), "accent": (250, 45, 72, 255),
                    "accent_icon": (255, 255, 255, 255), "shadow": 70}
        return {"bg": (24, 24, 24, 238), "edge": (255, 255, 255, 38), "icon": (255, 255, 255, 235),
                "sep": (255, 255, 255, 55), "hot": (255, 255, 255, 40), "accent": (30, 215, 96, 255),
                "accent_icon": (0, 0, 0, 255), "shadow": 120}

    def _pill_shadow(self, w, h):
        """Soft shadow under a pill of this size (cached)."""
        key = ("shadow", w, h, self.s.get("ui_style"))
        if key not in self._pills:
            m = round(10 * self.scale)
            mask = Image.new("L", (w + 2 * m, h + 2 * m), 0)
            ImageDraw.Draw(mask).rounded_rectangle((m, m + 2, m + w - 1, m + h + 1), h // 2,
                                                   fill=self._pill_colors()["shadow"])
            img = Image.new("RGBA", mask.size, (0, 0, 0, 0))
            img.putalpha(mask.filter(ImageFilter.GaussianBlur(m * 0.45)))
            self._pills[key] = img
        return self._pills[key]

    def _pill(self, hot):
        """Image of the control pill and each button's box inside it."""
        locked = self.s["lock_position"]
        style = self.s.get("ui_style")
        key = (self.playing, hot, self.scale, locked, style)
        if key in self._pills:
            return self._pills[key]
        col = self._pill_colors()
        bs, pad, sep = round(30 * self.scale), round(4 * self.scale), round(10 * self.scale)
        w, h = self._pill_size()
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=col["bg"],
                                              outline=col["edge"])
        top = Image.new("RGBA", (w, h), (0, 0, 0, 0))   # blended on top (see _draw_progress)
        d = ImageDraw.Draw(top)
        font = _icon_font(round(bs * 0.42))
        boxes, x = {}, pad
        for c in CONTROLS:
            if c is None:
                d.line((x + sep // 2, pad + bs * 0.25, x + sep // 2, pad + bs * 0.75),
                       fill=col["sep"], width=max(1, round(self.scale)))
                x += sep
                continue
            name, glyph = c
            if name == "play" and self.playing:
                glyph = PAUSE_GLYPH
            if name == "lock" and locked:
                glyph = UNLOCK_GLYPH
            box = (x, pad, x + bs, pad + bs)
            boxes[name] = box
            icon = col["icon"]
            if name == "play":                     # play / pause: a filled accent circle
                inset = round(1.5 * self.scale) if hot == name else round(2.5 * self.scale)
                d.ellipse((box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset), fill=col["accent"])
                icon = col["accent_icon"]
            elif hot == name:
                d.ellipse(box, fill=col["hot"])
            if font is not None:
                d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), glyph, font=font,
                       fill=icon, anchor="mm")
            x += bs
        img.alpha_composite(top)
        self._pills[key] = (img, boxes)
        return img, boxes

    def _pill_origin(self) -> tuple[int, int]:
        """Top-left of the pill on the canvas: above the bar's top-right corner, or below
        the bar when it's at the very top of the screen."""
        mx, mt, _ = self._margins()
        w, h = self._box
        pw, ph = self._pill_size()
        gap = round(4 * self.scale)
        x = mx + w - pw - round(12 * self.scale)
        if self.s["y"] - ph - gap < 0:
            return x, mt + h + gap
        return x, mt - ph - gap

    def flash(self, text: str, seconds: float = 2.0):
        """A short message next to the controls (e.g. "Lyrics 0.25 s earlier")."""
        self._flash = (text, time.monotonic() + seconds)
        self.redraw()

    _flash = None

    def _flash_alpha(self) -> float:
        if not self._flash:
            return 0.0
        left = self._flash[1] - time.monotonic()
        return max(0.0, min(1.0, left / 0.4))

    def _draw_flash(self, canvas):
        a = self._flash_alpha()
        if a <= 0:
            self._flash = None
            return
        text = self._flash[0]
        font = render.fonts.load("Segoe UI", False, round(13 * self.scale))
        pw, ph = self._pill_size()
        tw = round(font.getlength(text)) + round(24 * self.scale)
        col = self._pill_colors()
        img = Image.new("RGBA", (tw, ph), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, tw - 1, ph - 1), ph // 2, fill=col["bg"], outline=col["edge"])
        d.text((tw / 2, ph / 2), text, font=font, fill=col["icon"], anchor="mm")
        ox, oy = self._pill_origin()
        x = ox - tw - round(6 * self.scale) if self.hover > 0.01 else ox + pw - tw
        sh = self._pill_shadow(tw, ph)
        render.draw_items(canvas, [render.Item(sh, x + tw / 2, oy + ph / 2, a),
                                   render.Item(img, x + tw / 2, oy + ph / 2, a)])

    def _draw_controls(self, canvas):
        self._draw_flash(canvas)
        if self.hover <= 0.01 or not self.s["hover_controls"]:
            return
        img, _ = self._pill(self._hover_hot)
        ox, oy = self._pill_origin()
        sh = self._pill_shadow(img.width, img.height)
        render.draw_items(canvas, [render.Item(sh, ox + img.width / 2, oy + img.height / 2, self.hover),
                                   render.Item(img, ox + img.width / 2, oy + img.height / 2,
                                               self.hover)])

    def _button_at(self, x, y):
        """Name of the control at canvas point (x, y), if the controls are showing."""
        if self.hover < 0.5 or not self._controls_on():
            return None
        ox, oy = self._pill_origin()
        for name, (x0, y0, x1, y1) in self._pill(None)[1].items():
            if ox + x0 <= x < ox + x1 and oy + y0 <= y < oy + y1:
                return name
        return None

    def _cursor(self):
        """Mouse position on the canvas (window coordinates)."""
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        mx, mt, _ = self._margins()
        return pt.x - (int(self.s["x"]) - mx), pt.y - (int(self.s["y"]) - mt)

    def _over_controls_zone(self, x, y) -> bool:
        """Mouse over the bar, the controls, or the little gap between them."""
        mx, mt, _ = self._margins()
        w, h = self._box
        if mx <= x < mx + w and mt <= y < mt + h:
            return True
        ox, oy = self._pill_origin()
        pw, ph = self._pill_size()
        top, bottom = (oy, mt) if oy < mt else (mt + h, oy + ph)   # pill above / below
        return ox <= x < ox + pw and top <= y < bottom

    def _hover_target(self, now) -> float:
        active = self.visible and self._controls_on()
        return 1.0 if active and now - self._hover_seen < HOVER_LINGER else 0.0

    def _update_hover(self) -> bool:
        """Show the controls while the mouse is over the bar or the controls; they stay
        HOVER_LINGER seconds after it leaves, then fade. Returns True if anything changed."""
        now = time.monotonic()
        dt, self._hover_t = now - self._hover_t, now
        if not (self.visible and self.s["hover_controls"]):
            changed = self.hover > 0
            self.hover, self._hover_hot = 0.0, None
            return changed
        x, y = self._cursor()
        over = self._over_controls_zone(x, y)
        if self.s["lock_position"]:
            self._update_peek(now, over)
        if over and self._controls_on():
            self._hover_seen = now
        target = self._hover_target(now)
        old = (self.hover, self._hover_hot)
        step = dt / HOVER_FADE
        if target > self.hover:
            self.hover = min(target, self.hover + step)
        else:
            self.hover = max(target, self.hover - step)
        self._hover_hot = self._button_at(x, y)
        return (self.hover, self._hover_hot) != old

    def _update_peek(self, now, over):
        """Locked bar: Ctrl with the mouse over it wakes the controls and makes the bar
        clickable. Ctrl can then be let go; it goes back to click-through once the
        controls have faded (HOVER_LINGER after the mouse left)."""
        if self._ctrl_down() and over:
            self._hover_seen = now
            peek = True
        else:
            peek = self._peek and (now - self._hover_seen < HOVER_LINGER or self.hover > 0)
        if peek != self._peek:
            self._peek = peek
            self.layer.set_click_through(not peek)

    @staticmethod
    def _ctrl_down() -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)

    def _hover_moving(self) -> bool:
        if not (self.s["hover_controls"] and self.visible):
            return False
        target = self._hover_target(time.monotonic())
        return abs(self.hover - target) > 1e-3

    # ---- bouncing note ----------------------------------------------------
    def _note_img(self, glyph: str):
        st = self._style("cur")
        return render.text_image(glyph, render.TextStyle(
            "Segoe UI Symbol", False, round(st.px * 0.8), st.color, st.outline, st.outline_color,
            st.outline_px), 200)

    def _note_item(self):
        """The note at this moment: falls in at the line's start, hops from word to word as
        each is sung, and flies off upwards when the line is done."""
        if not self._note_window():
            return None
        _, _, starts, end = self.note[:4]
        real = self.note[4] if len(self.note) > 4 else True
        img, cy = getattr(self, "_slots", {}).get("cur", (None, 0))
        if img is None:
            return None
        st = self._style("cur")
        stroke = st.outline_px if st.outline else 0
        line = render.text_image(self.view.cur, st, self._max_w)
        boxes = render.word_boxes(self.view.cur, st.family, st.bold, st.px, stroke, self._max_w)
        n = min(len(boxes), len(starts))
        if n == 0 or line is None:
            return None
        left = (self.width - img.width) / 2 + (img.width - line.width) / 2
        top = cy - img.height / 2
        note_h = self._note_img(NOTES[0]).height

        def sit(j):  # note centre, resting just above the letters of word j
            bx, by, _ = boxes[j]
            return left + bx, top + by - note_h * 0.42

        p = self._note_time()

        def hop_len(k):  # how long the hop onto word k takes (0 = no hop)
            if k <= 0 or k >= n:
                return 0.0
            h = min(0.22, 0.6 * (starts[k] - starts[k - 1]))
            return h if h > 0.03 else 0.0

        def item(glyph, x, y, squash=0.0, alpha=1.0, angle=0.0):
            """squash > 0: flattened and wider; < 0: stretched tall. The bottom stays put."""
            img = self._note_img(glyph)
            sx, sy = 1 + squash * 0.8, 1 - squash
            return render.Item(img, x, y + img.height * (1 - sy) / 2, alpha, sx, sy, angle)

        def landed_at(k):  # when the note arrived on word k
            return starts[0] + NOTE_FALL if k == 0 or not hop_len(k) else starts[k]

        def takeoff(u, crouch):  # squash in the air: from the crouch into a tall stretch
            return crouch * max(0.0, 1 - u) ** 3 - 0.3 * math.sin(math.pi * min(1.0, u))

        if p > end:                                  # line finished: crouch, then spring upwards
            x, y = sit(n - 1)
            glyph = NOTES[(n - 1) % 4]
            t = p - end
            if t < NOTE_CROUCH:
                return item(glyph, x, y, max(_jelly(p - landed_at(n - 1)), 0.3 * t / NOTE_CROUCH))
            u = min(1.0, (t - NOTE_CROUCH) / NOTE_EXIT)
            y = y + (-note_h - y) * u * u
            return item(glyph, x, y, takeoff(u / 2, 0.3), 1 - u * u)

        j = max((k for k in range(n) if starts[k] <= p), default=0)
        if not real and p >= starts[0] + NOTE_FALL:
            # No real word timing for this line: don't pretend with confident hops. Glide
            # evenly along the words instead, bobbing gently.
            if j + 1 < n and starts[j + 1] > starts[j]:
                u = min(1.0, (p - starts[j]) / (starts[j + 1] - starts[j]))
                (x1, y1), (x2, y2) = sit(j), sit(j + 1)
                x, y = x1 + (x2 - x1) * u, y1 + (y2 - y1) * u
            else:
                x, y = sit(j)
            t = p - starts[0] - NOTE_FALL
            landed = _jelly(t) if t < 0.9 else 0.0
            bob = math.sin(2 * math.pi * 1.2 * t) * min(1.0, t / 0.5)
            return item(NOTES[j % 4], x, y - note_h * 0.06 * bob, landed + 0.03 * bob, 1.0, -4 * bob)
        x, y = sit(j)
        glyph = NOTES[j % 4]
        # When the note last landed: the drop-in for word 0, otherwise the hop onto word j.
        landed = landed_at(j)
        soft = min(1.0, hop_len(j) / 0.22) if j and hop_len(j) else 1.0   # after a quick hop
        squash = _jelly(p - landed) * soft if p >= landed else 0.0
        angle = 0.0
        hop = hop_len(j + 1)
        if hop and p > starts[j + 1] - hop - NOTE_CROUCH:
            t0 = starts[j + 1] - hop
            if p < t0:                               # crouch before jumping
                squash = max(squash, 0.25 * min(1.0, hop / 0.22) * (1 - (t0 - p) / NOTE_CROUCH))
            else:                                    # in the air: stretched, leaning forward
                u = (p - t0) / hop
                x2, y2 = sit(j + 1)
                x, y = x + (x2 - x) * u, y + (y2 - y) * u - note_h * 0.6 * math.sin(math.pi * u)
                k = min(1.0, hop / 0.22)             # quick hops squish less
                squash = takeoff(u, max(_jelly(t0 - landed), 0.25 * k)) * k
                angle = -14 * math.sin(math.pi * u) * (1 if x2 >= x else -1)
                glyph = NOTES[(j + 1) % 4] if u > 0.5 else glyph
        if p < starts[0] + NOTE_FALL:                # line just started: fall from the top
            u = max(0.0, (p - starts[0]) / NOTE_FALL)
            y = -note_h + (y + note_h) * u * u
            squash = -0.3 * math.sin(math.pi * u)    # stretches as it falls, round again on touchdown
        elif angle == 0.0 and p >= landed:           # gentle breathing while it sits
            squash += 0.05 * math.sin(2 * math.pi * 1.6 * (p - landed)) * min(1.0, (p - landed) / 0.5)
        return item(glyph, x, y, squash, 1.0, angle)

    # ---- dragging & clicks -------------------------------------------------
    def _press(self, e):
        self._pressed = self._button_at(e.x, e.y)
        if self._pressed is None:
            if self.s["lock_position"]:
                self._pressed = ""           # locked: only the controls respond
            self._dx, self._dy = e.x_root - self.s["x"], e.y_root - self.s["y"]

    def _drag(self, e):
        if self._pressed is not None:
            return
        self.s["x"], self.s["y"] = e.x_root - self._dx, e.y_root - self._dy
        self.redraw()

    def _release(self, e):
        name, self._pressed = self._pressed, None
        if name is None:
            self.on_moved()
        elif name and self._button_at(e.x, e.y) == name:
            self.on_control(name)
