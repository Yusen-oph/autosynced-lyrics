"""`Autosynced Lyrics.exe --selfcheck`: verify a build has everything it needs, then exit.

Writes a report to %TEMP%\\autosynced_lyrics_selfcheck.txt (a windowed app has no console).
"""
import os
import sys
import time
import traceback
from pathlib import Path

REPORT = Path(os.environ.get("TEMP", ".")) / "autosynced_lyrics_selfcheck.txt"


def _checks():
    def libraries():
        import ctranslate2
        import faster_whisper  # noqa: F401
        import numpy  # noqa: F401
        import pyaudiowpatch  # noqa: F401
        import pystray  # noqa: F401
        return f"ok (cpu compute types: {sorted(ctranslate2.get_supported_compute_types('cpu'))})"

    def audio_loopback():
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        try:
            n = sum(1 for _ in p.get_loopback_device_info_generator())
        finally:
            p.terminate()
        return f"{n} loopback device(s)"

    def media_controls():
        from media_watcher import MediaWatcher
        w = MediaWatcher()
        w.start()
        time.sleep(1.5)
        if w.error:
            raise RuntimeError(w.error)
        t = w.track
        return f"{t.player}: playing={t.playing}" if t else "no music app open"

    def fonts_and_render():
        import fonts
        import render
        style = render.TextStyle("Segoe UI", True, 32, "#ffffff", True, "#000000", 2)
        img = render.text_image("Hello 你好 ♪", style, 800)
        return f"{len(fonts.families())} font families, rendered {img.size}"

    def data_folder():
        import paths
        paths.prepare()
        probe = paths.DATA_DIR / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return f"writable: {paths.DATA_DIR}"

    def lyrics_parsing():
        import lyrics
        return f"{len(lyrics.parse_lrc('[00:01.00]a\\n[00:02.50]b'))} lines parsed"

    return [libraries, audio_loopback, media_controls, fonts_and_render, data_folder, lyrics_parsing]


def run() -> int:
    lines, failed = [f"frozen={getattr(sys, 'frozen', False)} python={sys.version.split()[0]}"], 0
    for check in _checks():
        try:
            lines.append(f"PASS {check.__name__}: {check()}")
        except Exception:
            failed += 1
            lines.append(f"FAIL {check.__name__}:\n{traceback.format_exc()}")
    lines.append("RESULT: " + ("all passed" if not failed else f"{failed} failed"))
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return 1 if failed else 0
