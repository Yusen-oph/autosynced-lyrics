"""Draws lyric lines (with outline) into RGBA images and composes animation frames."""
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

import fonts

# CJK characters can break anywhere; other text breaks at spaces.
_WIDE = r"⺀-鿿가-힯぀-ヿ＀-￯"
_TOKEN = re.compile(rf"[{_WIDE}]|[^\s{_WIDE}]+|\s+")


def _wrap(text: str, font, max_w: float) -> list[str]:
    rows, cur = [], ""
    for tok in _TOKEN.findall(text):
        if cur.strip() and not tok.isspace() and font.getlength(cur + tok) > max_w:
            rows.append(cur.rstrip())
            cur = tok
        else:
            cur += tok
    rows.append(cur.strip())
    return [r for r in rows if r] or [""]


@dataclass(frozen=True)
class TextStyle:
    family: str
    bold: bool
    px: int
    color: str
    outline: bool
    outline_color: str
    outline_px: int


@lru_cache(maxsize=512)
def text_image(text: str, style: TextStyle, max_w: int) -> Image.Image | None:
    """Centered, wrapped, outlined text on a transparent background."""
    if not text:
        return None
    return colorize(text_masks(text, style.family, style.bold, style.px,
                               style.outline_px if style.outline else 0, max_w),
                    style.color, style.outline_color)


def colorize(masks, color: str, outline_color: str) -> Image.Image:
    fill_mask, stroke_mask = masks
    fill = Image.new("RGBA", fill_mask.size, color)
    fill.putalpha(fill_mask)
    if stroke_mask is None:
        return fill
    out = Image.new("RGBA", fill_mask.size, outline_color)
    out.putalpha(stroke_mask)
    return Image.alpha_composite(out, fill)


@lru_cache(maxsize=512)
def text_masks(text: str, family: str, bold: bool, px: int, stroke: int, max_w: int):
    """The text's shape (fill mask, outline mask or None), independent of colour."""
    font = fonts.font_for(text, family, bold, px)
    rows = _wrap(text, font, max_w - 2 * stroke - 4)
    ascent, descent = font.getmetrics()
    row_h, gap = ascent + descent, int((ascent + descent) * 0.08)
    widths = [font.getlength(r) for r in rows]
    w = int(max(widths)) + 2 * stroke + 4
    h = row_h * len(rows) + gap * (len(rows) - 1) + 2 * stroke

    # Draw into masks, then colour them: avoids dark fringes on transparent pixels.
    fill_mask = Image.new("L", (w, h), 0)
    stroke_mask = Image.new("L", (w, h), 0)
    fd, sd = ImageDraw.Draw(fill_mask), ImageDraw.Draw(stroke_mask)
    y = stroke
    for row, rw in zip(rows, widths):
        x = (w - rw) / 2
        fd.text((x, y), row, font=font, fill=255)
        if stroke:
            sd.text((x, y), row, font=font, fill=255, stroke_width=stroke, stroke_fill=255)
        y += row_h + gap
    return fill_mask, (stroke_mask if stroke else None)


TR_GAP = 2  # px between a line and its translation (scaled by caller)


@lru_cache(maxsize=32)
def badge(text: str, px: int, color: str, outline_color: str, scale: int = 1) -> Image.Image:
    """A small pill tag like (AI): dark fill, coloured border and letters, readable on any
    background (light or dark, with or without a box)."""
    font = fonts.load("Segoe UI", True, px)
    left, top, right, bottom = font.getbbox(text)
    tw, th = right - left, bottom - top
    pad_x, pad_y, line = round(px * 0.45), round(px * 0.28), max(1, scale)
    w, h = tw + 2 * pad_x + 2 * line, th + 2 * pad_y + 2 * line
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=(20, 20, 20, 200), outline=color,
                        width=line)
    d.text(((w - tw) / 2 - left, (h - th) / 2 - top), text, font=font, fill=color)
    return img


def hstack(left: Image.Image | None, right: Image.Image, gap: int) -> Image.Image:
    """`right` placed after `left`, vertically centred; the pair stays centred as a whole."""
    if left is None:
        return right
    w, h = left.width + gap + right.width, max(left.height, right.height)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(left, (0, (h - left.height) // 2))
    out.alpha_composite(right, (left.width + gap, (h - right.height) // 2))
    return out


def stack(top: Image.Image | None, bottom: Image.Image | None, gap: int) -> Image.Image | None:
    """`top` with `bottom` centred under it (a lyric line and its translation)."""
    if bottom is None:
        return top
    if top is None:
        return bottom
    w = max(top.width, bottom.width)
    out = Image.new("RGBA", (w, top.height + gap + bottom.height), (0, 0, 0, 0))
    out.alpha_composite(top, ((w - top.width) // 2, 0))
    out.alpha_composite(bottom, ((w - bottom.width) // 2, top.height + gap))
    return out


@lru_cache(maxsize=256)
def word_boxes(text: str, family: str, bold: bool, px: int, stroke: int, max_w: int):
    """Where each word sits inside text_image(text): [(x_centre, top_of_letters, row_height)].

    One entry per non-space token, in order, using the same wrapping as text_masks.
    "top_of_letters" is where the tall letters start, not the font's (often roomy) line top.
    """
    font = fonts.font_for(text, family, bold, px)
    rows = _wrap(text, font, max_w - 2 * stroke - 4)
    ascent, descent = font.getmetrics()
    row_h, gap = ascent + descent, int((ascent + descent) * 0.08)
    widths = [font.getlength(r) for r in rows]
    w = int(max(widths)) + 2 * stroke + 4
    out, y = [], stroke
    for row, rw in zip(rows, widths):
        x0 = (w - rw) / 2
        for m in _TOKEN.finditer(row):
            if m.group().isspace():
                continue
            left = x0 + font.getlength(row[:m.start()])
            bbox = font.getbbox(m.group())
            out.append((left + font.getlength(m.group()) / 2, y + bbox[1] - stroke, row_h))
        y += row_h + gap
    return out


def tokens(text: str) -> list[tuple[int, str]]:
    """Non-space words/characters with their offsets (CJK characters count separately)."""
    return [(m.start(), m.group()) for m in _TOKEN.finditer(text) if not m.group().isspace()]


def blank_height(style: TextStyle) -> int:
    ascent, descent = fonts.load(style.family, style.bold, style.px).getmetrics()
    return ascent + descent + 2 * (style.outline_px if style.outline else 0)


# ---- frame composition -----------------------------------------------------
@dataclass
class Item:
    img: Image.Image
    cx: float
    cy: float
    alpha: float = 1.0
    sx: float = 1.0
    sy: float = 1.0
    angle: float = 0.0


def _transform(it: Item) -> Image.Image | None:
    img = it.img
    if it.alpha <= 0.01 or abs(it.sx) < 0.02 or abs(it.sy) < 0.02:
        return None
    if abs(it.sx - 1) > 1e-3 or abs(it.sy - 1) > 1e-3:
        img = img.resize((max(1, round(img.width * abs(it.sx))), max(1, round(img.height * abs(it.sy)))),
                         Image.BILINEAR)
    if it.angle:
        img = img.rotate(it.angle, resample=Image.BICUBIC, expand=True)
    if it.alpha < 1:
        img = img.copy()
        img.putalpha(img.getchannel("A").point(lambda v, a=it.alpha: int(v * a)))
    return img


def compose(size: tuple[int, int], items: list[Item], bg_rgba: tuple[int, int, int, int],
            radius: int) -> Image.Image:
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    if bg_rgba[3]:
        ImageDraw.Draw(canvas).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=bg_rgba)
    return draw_items(canvas, items)


@lru_cache(maxsize=8)
def box_background(size: tuple[int, int], rgba: tuple, radius: int, soft: bool) -> Image.Image:
    """The rounded background box. soft: slightly lighter at the top, fading to the chosen
    colour, with a faint highlight along the top edge (a gentle, glassy look)."""
    w, h = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    if not rgba[3]:
        return canvas
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=255)
    if not soft or rgba[3] < 20:
        ImageDraw.Draw(canvas).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=rgba)
        return canvas
    r, g, b, a = rgba
    top = tuple(min(255, round(c + (255 - c) * 0.10)) for c in (r, g, b))
    grad = Image.new("RGBA", (1, h))
    for y in range(h):
        e = min(1.0, y / max(1, h * 0.7))
        grad.putpixel((0, y), (*(round(t + (c - t) * e) for t, c in zip(top, (r, g, b))), a))
    fill = grad.resize(size)
    edge = Image.new("L", size, 0)        # 1 px light line just inside the top edge
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, w - 1, h - 1), radius, outline=255, width=1)
    edge = Image.composite(edge, Image.new("L", size, 0),
                           Image.linear_gradient("L").rotate(180).resize(size))
    shine = Image.new("RGBA", size, (255, 255, 255, 0))
    shine.putalpha(edge.point(lambda v: v * 50 // 255))
    canvas.paste(fill, (0, 0), mask)
    canvas.alpha_composite(shine)
    canvas.putalpha(Image.composite(canvas.getchannel("A"), Image.new("L", size, 0), mask))
    return canvas


@lru_cache(maxsize=8)
def drop_shadow(box: tuple[int, int], radius: int, margins: tuple[int, int, int], strength: int,
                drop: int) -> Image.Image:
    """Transparent canvas (box + margins) with a soft shadow where the box will sit.
    margins = (left/right, top, bottom); strength = shadow opacity 0-255."""
    w, h = box
    mx, mt, mb = margins
    size = (w + 2 * mx, h + mt + mb)
    mask = Image.new("L", size, 0)
    if strength > 0:
        ImageDraw.Draw(mask).rounded_rectangle((mx, mt + drop, mx + w - 1, mt + h - 1 + drop),
                                               radius, fill=strength)
        mask = mask.filter(ImageFilter.GaussianBlur(max(1, mx * 0.45)))
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.putalpha(mask)
    return out


def with_glow(img: Image.Image, radius: int, strength: int) -> Image.Image:
    """A soft dark glow behind the text (for text floating without a background box).
    Blurred at half size: looks the same, about 4x faster."""
    a = img.getchannel("A")
    small = a.resize((max(1, a.width // 2), max(1, a.height // 2)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(max(1, radius / 2)))
    glow = small.resize(a.size, Image.BILINEAR).point(lambda v: min(255, v * 3) * strength // 255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.putalpha(glow)
    out.alpha_composite(img)
    return out


def compose_on(bg: Image.Image, items: list[Item]) -> Image.Image:
    return draw_items(bg.copy(), items)


def draw_items(canvas: Image.Image, items: list[Item]) -> Image.Image:
    size = canvas.size
    for it in items:
        img = _transform(it)
        if img is None:
            continue
        x, y = round(it.cx - img.width / 2), round(it.cy - img.height / 2)
        # crop to canvas (alpha_composite needs in-bounds coordinates)
        sx0, sy0 = max(0, -x), max(0, -y)
        sx1, sy1 = min(img.width, size[0] - x), min(img.height, size[1] - y)
        if sx1 <= sx0 or sy1 <= sy0:
            continue
        canvas.alpha_composite(img, (x + sx0, y + sy0), (sx0, sy0, sx1, sy1))
    return canvas


def ease(t: float) -> float:
    return 0.5 - math.cos(math.pi * max(0.0, min(1.0, t))) / 2
