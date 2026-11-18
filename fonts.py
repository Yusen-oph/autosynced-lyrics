"""Finds installed Windows font files (Pillow needs files, not family names) with CJK fallback."""
import os
import re
import winreg
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

_FONT_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_SYS_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_USER_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"

_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
_CJK = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")
_SYMBOL = re.compile(r"[\u2600-\u27bf]")

KOREAN_FONTS = ("Malgun Gothic", "Gulim")
CJK_FONTS = ("Microsoft YaHei UI", "Microsoft YaHei", "DengXian", "SimHei",
             "Microsoft JhengHei UI", "Yu Gothic UI", "Meiryo UI", "MS Gothic", "SimSun")
SYMBOL_FONTS = ("Segoe UI Symbol",)


@lru_cache(maxsize=1)
def families() -> dict[str, dict[str, tuple[str, int]]]:
    """{family: {"regular": (path, ttc_index), "bold": (...)}} for installed, non-italic fonts."""
    found: dict[str, dict[str, tuple[str, int]]] = {}
    for hive, base in ((winreg.HKEY_LOCAL_MACHINE, _SYS_DIR), (winreg.HKEY_CURRENT_USER, _USER_DIR)):
        try:
            key = winreg.OpenKey(hive, _FONT_KEY)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, file, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(file, str) or not file.lower().endswith((".ttf", ".ttc", ".otf")):
                    continue
                path = Path(file) if os.path.isabs(file) else base / file
                if not path.exists():
                    continue
                name = re.sub(r"\s*\(.*?\)\s*$", "", name)
                for idx, face in enumerate(p.strip() for p in name.split("&")):
                    if re.search(r"\b(Italic|Oblique)\b", face):
                        continue
                    style = "regular"
                    if face.endswith(" Bold"):
                        face, style = face[:-5], "bold"
                    found.setdefault(face, {}).setdefault(style, (str(path), idx))
    return {f: v for f, v in found.items() if "regular" in v}


def family_names() -> list[str]:
    return sorted(families(), key=str.lower)


def _first_available(names) -> str | None:
    fams = families()
    return next((n for n in names if n in fams), None)


@lru_cache(maxsize=128)
def load(family: str, bold: bool, px: int) -> ImageFont.FreeTypeFont:
    fams = families()
    fam = fams.get(family) or fams.get("Segoe UI") or fams.get("Arial")
    if fam is None:
        return ImageFont.load_default(size=px)
    path, idx = fam.get("bold" if bold else "regular") or fam["regular"]
    try:
        return ImageFont.truetype(path, px, index=idx)
    except OSError:
        return ImageFont.truetype(path, px)


def font_for(text: str, family: str, bold: bool, px: int) -> ImageFont.FreeTypeFont:
    """Pick `family`, or a fallback that actually has glyphs for the text's script."""
    if _HANGUL.search(text) and family not in KOREAN_FONTS:
        family = _first_available(KOREAN_FONTS) or family
    elif _CJK.search(text) and family not in CJK_FONTS:
        family = _first_available(CJK_FONTS) or family
    elif _SYMBOL.search(text) and family not in SYMBOL_FONTS:
        family = _first_available(SYMBOL_FONTS) or family
    return load(family, bold, px)
