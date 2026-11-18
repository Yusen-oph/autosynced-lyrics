"""Colours for the lyrics bar taken from the album cover."""
import colorsys
import io
from functools import lru_cache

from PIL import Image


@lru_cache(maxsize=16)
def from_cover(data: bytes, light: bool = False) -> dict | None:
    """{"cur": text colour, "bg": background colour} from the cover's most striking colour,
    or None for a black-and-white / unreadable cover (then the user's own colours are used)."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((40, 40))
    except Exception:
        return None
    bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(24)]       # per hue: weight, r, g, b
    raw = img.tobytes()
    for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3]):
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.25 or v < 0.25:
            continue                                        # greys, blacks: not a colour
        w = s * v * v
        k = bins[int(h * 24) % 24]
        k[0] += w; k[1] += r * w; k[2] += g * w; k[3] += b * w
    # Neighbouring hues count together, so one colour isn't split across two bins.
    score = [bins[i][0] + 0.5 * (bins[i - 1][0] + bins[(i + 1) % 24][0]) for i in range(24)]
    best = max(range(24), key=score.__getitem__)
    wsum = bins[best][0]
    if wsum < 40 * 40 * 0.02:
        return None
    r, g, b = (bins[best][c] / wsum / 255 for c in (1, 2, 3))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if light:   # light look (Day style): deep coloured text on a pale tint
        cur = colorsys.hls_to_rgb(h, min(l, 0.32), min(1.0, max(s, 0.6)))
        bg = colorsys.hls_to_rgb(h, 0.94, min(0.6, s * 0.7))
    else:
        cur = colorsys.hls_to_rgb(h, max(l, 0.74), min(1.0, max(s, 0.55)))   # bright, easy to read
        bg = colorsys.hls_to_rgb(h, 0.13, min(0.55, s * 0.6))                # dark, tinted
    hexc = lambda c: "#" + "".join(f"{round(x * 255):02x}" for x in c)
    return {"cur": hexc(cur), "bg": hexc(bg)}
