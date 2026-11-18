# Autosynced Lyrics

Windows tray app that floats time-synced lyrics for whatever Spotify, Apple Music, NetEase Cloud Music or QQ Music is playing over your desktop.

## Download

Get **AutosyncedLyrics-Windows.zip** from the [latest release](https://github.com/Yusen-oph/autosynced-lyrics/releases/latest),
unzip it, run `Autosynced Lyrics.exe` and choose **Yes** to install (Start menu, desktop shortcut,
Settings → Apps). Windows may warn that the app isn't signed: *More info → Run anyway*.

**Updates:** the app checks this repo's releases when it starts and every 6 hours (Settings → About
can turn that off or check now). When a new version is out, a notice appears in the app window and
the tray; **Update now** downloads only the small update (~17 MB) and restarts. If an update adds new
libraries, the notice links to the full download instead.

**Publishing a release** (maintainer): bump `VERSION` in `installer.py`, then run
`.venv\Scripts\python release.py "what changed"`. It builds the app and uploads the three files
(`update.json`, `AutosyncedLyrics-Update.zip`, `AutosyncedLyrics-Windows.zip`) as release `vX.Y`.

- **Now playing + position**: Windows media controls (`winrt`), so no developer account or login is needed.
  Works with Spotify, the Apple Music app for Windows, and the NetEase Cloud Music / QQ Music desktop
  apps if their version reports to Windows' media controls (Settings → Behavior → Music app: automatic
  or one app only). Apple Music's "Artist — Album" field is split; if an app doesn't
  report a moving position, the app times the song itself from its start (skipping within a song
  then needs the lyrics offset or the editor). The Apple Music website in a browser isn't supported.
- **Lyrics**: [LRCLIB](https://lrclib.net) first. Only if it has no synced lyrics: NetEase, QQ Music
  (both skipped for 10 minutes whenever they're unreachable, e.g. blocked by a school filter), Kugou
  (synced, strong on Chinese/Japanese songs), then lyrics.ovh (untimed, editor only, as a
  starting point for Whisper or tap-sync). Versions more than 10–15 s longer or shorter than
  the Spotify track are rejected. Lyrics found online go into a small cache (`lyrics_cache.db`, max 5 MB, refreshed after 60 days; can be turned off or cleared in Settings → Behavior); only lyrics you sync yourself are saved permanently (in `my_lyrics/`).
- **UI**: tkinter overlay + settings window, `pystray` tray icon.

## Setup

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw main.py
```

## Using it

- The app lives in the system tray. The lyrics bar appears only while a supported music app is open (configurable).
- **Settings**: double-click the tray icon, right-click the lyrics bar, or launch the app again
  (e.g. the desktop shortcut). Only one copy ever runs; launching again just opens settings.
- Settings apply live and are saved to `settings.json` the moment you change them (kept across
  restarts): opacity, background opacity (0 = text only), font, black (or any color) text outline,
  song title & artist header, sizes, colors, previous/next lines, line-change animation
  (fade, slide up, turn over, spin, zoom, swing), width, timing offset, lock & click-through.
- **No synced lyrics?** Right-click the bar → *Edit / sync lyrics…*. The editor looks the song up
  online (plain lyrics are often available even when synced ones aren't), or you paste them in.
  Play the song, press *Start tap-sync*, and hit Space as each line starts. Saved to
  `my_lyrics/Artist - Title.lrc`, which always wins over online lyrics.
- **AI auto-sync**: in the editor, with untimed lyrics in the box, press *Listen & auto-sync*.
  The app records what your speakers play (WASAPI loopback, PyAudioWPatch) while the song runs,
  transcribes it with Whisper (faster-whisper, runs locally on the CPU, no account), and lines the
  lyric words up with the heard words. Lines it couldn't hear are estimated and shown in orange
  for you to check with tap-sync. Models download once into `models/` (base ~150 MB,
  small ~480 MB, medium ~1.5 GB). Don't seek/skip while it listens.
- **Copyright**: lyrics are copyrighted. Keep `my_lyrics/` for personal use and don't
  redistribute those files or bundle them with the app.
- **Open with Spotify**: tick *Start automatically when I log in to Windows*. The app then waits
  quietly in the tray and the lyrics show up whenever Spotify starts.
- Drag the lyrics bar to move it; its position is remembered.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | App entry: single-instance guard, main loop, lyric timing |
| `media_watcher.py` | Reads track + playback position (Spotify, Apple Music, NetEase, QQ Music) from Windows media controls |
| `cache.py` | Small cache of lyrics found online (max 5 MB, oldest dropped; Settings → Behavior) |
| `lyrics.py` | Lookup order, LRCLIB, LRC parsing, your saved lyrics |
| `sources.py` | Extra lyric sources (NetEase, QQ Music, Kugou, lyrics.ovh); Chinese translations |
| `overlay.py` | The always-on-top lyrics bar: layout and animations |
| `render.py` | Draws outlined text and composes frames (Pillow) |
| `layered.py` | Per-pixel transparent window (Win32 UpdateLayeredWindow) |
| `fonts.py` | Finds font files, falls back for Chinese/Japanese/Korean |
| `lyrics_editor.py` | Find, write, tap-sync and AI-sync lyrics |
| `autosync.py` | Audio capture, Whisper transcription, lyric alignment |
| `settings_window.py` | Settings UI |
| `tray.py` | Tray icon and app icon |
| `config.py` | Settings defaults/persistence, start-with-Windows registry entry |
