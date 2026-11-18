"""The listening jobs behind the app window's Editor page: AI auto-sync / transcribe, and
slow replay of a recorded line. No UI here; app_server.py exposes it to the page.

One job at a time:
    idle -> recording (Recorder captures the song) -> processing (Whisper) -> done | error
"""
import threading

import autosync


class EditorJobs:
    def __init__(self, watcher):
        self.watcher = watcher
        self.lock = threading.Lock()
        self.state = "idle"          # idle | recording | processing | done | error
        self.mode = "align"          # align (time the lyrics you have) | transcribe (write a draft)
        self.message = ""
        self.progress = 0.0
        self.result = None           # {"lines": [{"t", "text", "guess"}], "reason", ...}
        self.recorder: autosync.Recorder | None = None
        self.song = None
        self.slow_playing = False
        self._lines: list[str] = []
        self._model = "small"

    # ---- AI listening ---------------------------------------------------------
    def start(self, mode: str, lines: list[str], model: str, restart: bool, song) -> None:
        with self.lock:
            if self.state in ("recording", "processing"):
                raise RuntimeError("Already listening")
            t = self.watcher.track
            if t is None:
                raise RuntimeError("Start playing the song in your music app first.")
            if model not in autosync.MODEL_SIZES:
                model = "small"
            self.mode, self._lines, self._model, self.song = mode, list(lines), model, tuple(song)
            self.result, self.progress = None, 0.0
            self.recorder = autosync.Recorder(self.watcher)
            self.recorder.song = self.song
            self.state, self.message = "recording", "Listening… let the song play."
        if restart:
            self.watcher.restart()
            if not t.playing:
                self.watcher.toggle_play()
            threading.Timer(0.8, self.recorder.start).start()     # let the seek land first
        else:
            self.recorder.start()
        threading.Thread(target=self._wait_then_process, daemon=True).start()

    def finish(self) -> None:
        if self.state == "recording" and self.recorder:
            self.recorder.stop("finished early")

    def cancel(self) -> None:
        if self.recorder:
            self.recorder.stop("cancelled")
        self.state, self.message = "idle", ""

    def _wait_then_process(self):
        r = self.recorder
        while not (r.stop_flag.is_set() and not r.is_alive()):
            r.stop_flag.wait(0.2)
            if r.stop_flag.is_set():
                r.join(timeout=5)
        if self.state != "recording":
            return                                      # cancelled
        if r.error or r.seconds < 5:
            self.state, self.message = "error", r.error or "The recording was too short."
            return
        self.state, self.message, self.progress = "processing", "Listening to the recording…", 0.0

        def progress(frac, msg):
            self.progress, self.message = frac, msg
        try:
            words = autosync.transcribe(r.audio_16k(), self._model, progress)
            if self.mode == "align":
                times, guessed = autosync.align(self._lines, words, r.song_time)
                lines = [{"t": t, "text": l.strip(), "guess": bool(g)}
                         for l, t, g in zip(self._lines, times, guessed)]
            else:
                lines = [{"t": t, "text": text, "guess": False}
                         for t, text in autosync.draft_lyrics(words, r.song_time)]
            self.result = {"lines": lines, "reason": r.stop_reason, "mode": self.mode}
            self.state, self.message, self.progress = "done", "", 1.0
        except Exception as exc:
            self.state, self.message = "error", str(exc)[:300]

    def status(self) -> dict:
        r = self.recorder
        out = {"state": self.state, "mode": self.mode, "message": self.message,
               "progress": round(self.progress, 3),
               "seconds": round(r.seconds, 1) if r and self.state == "recording" else 0,
               "has_recording": bool(r and not r.is_alive() and r.seconds > 5),
               "recorded_song": list(self.song) if self.song else None,
               "slow_playing": self.slow_playing,
               "models": [{"name": n, "info": info,
                           "downloaded": (autosync.MODELS_DIR / f"models--Systran--faster-whisper-{n}").exists()}
                          for n, info in autosync.MODEL_SIZES.items()]}
        if self.state == "done" and self.result is not None:
            out["result"] = self.result
            self.result, self.state = None, "idle"          # handed over once
        return out

    # ---- slow replay of a recorded line ---------------------------------------
    def slow_replay(self, start: float, end: float, song) -> str:
        r = self.recorder
        if r is None or getattr(r, "song", None) != tuple(song) or r.is_alive():
            raise RuntimeError("Slow replay uses the app's recording: run Transcribe or Auto-sync "
                               "on this song first.")
        if self.slow_playing:
            return "Already playing."
        piece = r.snippet_16k(max(0.0, start - 0.3), end)
        if piece is None:
            raise RuntimeError("That part of the song wasn't recorded.")
        t = self.watcher.track
        if t and t.playing:
            self.watcher.toggle_play()        # pause the music so the two don't overlap
        self.slow_playing = True

        def work():
            try:
                autosync.play_audio(autosync.time_stretch(piece, 0.75))
            finally:
                self.slow_playing = False
        threading.Thread(target=work, daemon=True).start()
        return "Playing at 75% speed…"
