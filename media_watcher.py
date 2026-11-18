"""Reads what Spotify, Apple Music, NetEase Cloud Music or QQ Music is playing via Windows' media controls (no API keys)."""
import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from winrt.windows.storage.streams import DataReader
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

# player id -> (display name, substrings of the app id Windows reports)
PLAYERS = {
    "spotify": ("Spotify", ("spotify",)),
    "apple_music": ("Apple Music", ("applemusic", "itunes")),
    "netease": ("NetEase Cloud Music", ("cloudmusic", "netease", "163music")),
    "qq_music": ("QQ Music", ("qqmusic",)),
}
PLAYER_CHOICES = {  # label in settings -> setting value
    "Automatic (whichever is playing)": "auto",
    "Spotify only": "spotify",
    "Apple Music only": "apple_music",
    "NetEase Cloud Music only": "netease",
    "QQ Music only": "qq_music",
}


def player_of(app_id: str) -> str | None:
    app_id = (app_id or "").lower()
    return next((p for p, (_, keys) in PLAYERS.items() if any(k in app_id for k in keys)), None)


@dataclass
class Track:
    title: str
    artist: str
    album: str
    duration: float      # seconds (0 if the player doesn't say)
    position: float      # seconds, at the moment `anchored_at` was taken
    anchored_at: float   # time.monotonic() when position was measured
    playing: bool
    player: str = "Spotify"

    @property
    def key(self) -> tuple:
        return (self.title, self.artist)

    def current_position(self) -> float:
        if not self.playing:
            return self.position
        return self.position + (time.monotonic() - self.anchored_at)


class MediaWatcher(threading.Thread):
    """Polls the music app's media session in a background thread; read `.track`."""

    def __init__(self, interval: float = 0.5, player: str = "auto"):
        super().__init__(daemon=True)
        self.interval = interval
        self.player = player          # "auto" or a PLAYERS key; can change live
        self.track: Track | None = None
        self.error: str | None = None
        self.can_seek = False
        self._aloop = None
        self._session = None
        self._app_id = None
        # For players whose reported position doesn't move: our own clock per song.
        self._clock = None            # (song key, position, time.monotonic(), playing)
        self._last_raw = None         # (app id, song key, raw reported position)
        self._moving = set()          # (app id, song key) whose reported position does move
        self.cover = None             # (song key, image bytes or None): the album cover
        self._cover_tries: dict = {}
        self._last_cover = None

    def run(self):
        asyncio.run(self._loop())

    async def _loop(self):
        self._aloop = asyncio.get_running_loop()
        manager = await MediaManager.request_async()
        while True:
            try:
                self.track = await self._read(manager)
                self.error = None
            except Exception as exc:  # keep polling even if one read fails
                self.error = str(exc)
            await asyncio.sleep(self.interval)

    # ---- playback control (used by the lyrics editor) ----------------------
    def _run(self, action):
        session, loop = self._session, self._aloop
        if session is None or loop is None:
            return

        async def go():
            await action(session)
        asyncio.run_coroutine_threadsafe(go(), loop)

    def seek(self, seconds: float):
        ticks = int(max(0.0, seconds) * 10_000_000)  # WinRT TimeSpan = 100 ns units
        self._run(lambda s: s.try_change_playback_position_async(ticks))
        if self._clock:  # keep our own clock (if used) in step with the jump
            key, _, _, playing = self._clock
            self._clock = (key, max(0.0, seconds), time.monotonic(), playing)

    def restart(self):
        if self.can_seek:
            self.seek(0)
        else:
            self._run(lambda s: s.try_skip_previous_async())

    def toggle_play(self):
        self._run(lambda s: s.try_toggle_play_pause_async())

    def next_track(self):
        self._run(lambda s: s.try_skip_next_async())

    def previous_track(self):
        self._run(lambda s: s.try_skip_previous_async())

    async def _read_cover(self, key, props):
        """The album cover as image bytes, read once per song. The app may switch the cover a
        moment after the title, so a missing cover, or the previous song's, is retried a few times."""
        if self.cover and self.cover[0] == key:
            return
        tries = self._cover_tries.get(key, 0) + 1
        self._cover_tries = {key: tries}
        data = None
        try:
            if props.thumbnail is not None:
                stream = await props.thumbnail.open_read_async()
                reader = DataReader(stream.get_input_stream_at(0))
                await reader.load_async(stream.size)
                data = bytes(reader.read_buffer(stream.size))
        except Exception:
            data = None
        stale = data is not None and data == self._last_cover
        if (data is None or stale) and tries < 6:
            return                                   # try again on the next poll (0.5 s)
        self.cover = (key, data)
        if data is not None:
            self._last_cover = data

    # ---- reading -----------------------------------------------------------
    def _pick(self, manager):
        """The session to follow: a playing one first, staying with the current app if tied."""
        candidates = []
        for s in manager.get_sessions():
            p = player_of(s.source_app_user_model_id)
            if p and self.player in ("auto", p):
                playing = s.get_playback_info().playback_status == PlaybackStatus.PLAYING
                candidates.append((not playing, s.source_app_user_model_id != self._app_id, s, p))
        if not candidates:
            return None, None
        candidates.sort(key=lambda c: (c[0], c[1]))
        return candidates[0][2], candidates[0][3]

    async def _read(self, manager) -> Track | None:
        session, player = self._pick(manager)
        self._session = session
        if session is None:
            self._app_id = None
            return None
        self._app_id = session.source_app_user_model_id

        props = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        info = session.get_playback_info()
        playing = info.playback_status == PlaybackStatus.PLAYING
        self.can_seek = bool(info.controls.is_playback_position_enabled)

        title, artist, album = props.title or "", props.artist or "", props.album_title or ""
        if player == "apple_music" and " — " in artist:
            # Apple Music for Windows reports "Artist — Album" in the artist field.
            artist, from_artist_field = (part.strip() for part in artist.split(" — ", 1))
            album = album or from_artist_field

        duration = max(0.0, (timeline.end_time - timeline.start_time).total_seconds())
        position = timeline.position.total_seconds()
        key = (title, artist)
        await self._read_cover(key, props)

        if player != "spotify":
            raw = (self._app_id, key, position)
            if self._last_raw and self._last_raw[:2] == raw[:2] and self._last_raw[2] != position:
                self._moving.add(raw[:2])
            self._last_raw = raw
            if raw[:2] not in self._moving:
                position = self._own_clock(key, position, playing)
                return Track(title, artist, album, duration, position, time.monotonic(),
                             playing, PLAYERS[player][0])

        # The app only updates the timeline on play/pause/seek (Spotify) or every so often,
        # so extrapolate from the time of its last update.
        if playing:
            last = timeline.last_updated_time
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if 0 <= elapsed < max(duration, 1) * 2:
                position += elapsed

        return Track(title, artist, album, duration, position, time.monotonic(), playing,
                     PLAYERS[player][0])

    def _own_clock(self, key, reported: float, playing: bool) -> float:
        """Position counted by us from when the song started, pausing when paused."""
        now = time.monotonic()
        if self._clock is None or self._clock[0] != key:
            self._clock = (key, reported, now, playing)
            return reported
        _, pos, at, was_playing = self._clock
        if was_playing:
            pos += now - at
        self._clock = (key, pos, now, playing)
        return pos
