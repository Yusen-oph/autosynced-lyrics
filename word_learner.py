"""Learns word-by-word timing by listening, for songs no lyrics site has it for.

While a song plays whose lyric lines lack word timing, the computer's audio output is
recorded in the background (WASAPI loopback). Every CHUNK seconds the last stretch is run
through Whisper with word timestamps, the heard words are matched to the lyrics on screen,
and every line with enough matched words gets real per-word timing. The app shows it
straight away (the note starts hopping on the words) and keeps it in the lyrics cache.

Nothing is recorded to disk; audio is held in memory for under a minute.
"""
import queue
import threading
import time

import numpy as np

import autosync
import render

CHUNK = 20.0           # seconds of song per listening pass
OVERLAP = 1.5          # re-hear the end of the previous pass (words cut in half)
KEEP = 45.0            # seconds of audio kept in memory
MIN_MATCHED = 0.4      # share of a line's words that must be heard to trust its timing
BIAS = 0.1             # Whisper's word starts come slightly early


def _resample_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    if rate == 16000:
        return audio.astype(np.float32)
    if rate % 16000 == 0:
        k = rate // 16000
        return audio[: len(audio) // k * k].reshape(-1, k).mean(axis=1).astype(np.float32)
    n = int(len(audio) * 16000 / rate)
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


def model_name() -> str:
    """The Whisper model to use: one already downloaded if possible (base is lightest)."""
    for name in ("base", "small", "medium"):
        if (autosync.MODELS_DIR / f"models--Systran--faster-whisper-{name}").exists():
            return name
    return "base"


def have_model() -> bool:
    return any((autosync.MODELS_DIR / f"models--Systran--faster-whisper-{n}").exists()
               for n in ("base", "small", "medium"))


def word_slots(text: str) -> list[tuple[int, int, str]]:
    """The line's words as (char offset, length, normalised form for matching)."""
    out = []
    for off, tok in render.tokens(text):
        norm = "".join(autosync.tokens(tok))
        if norm:
            out.append((off, len(tok), norm))
    return out


def match_lines(lines: list[tuple[float, str]], wanted: set[int], heard, window) -> dict:
    """heard: [(word, song_start, song_end)]. Returns {line index: per-char (start, end)}
    for wanted lines inside `window` (song seconds) whose words were heard well enough."""
    lo, hi = window
    idx = [i for i in sorted(wanted)
           if i < len(lines) and lo - 2 <= lines[i][0] <= hi]
    if not idx or not heard:
        return {}
    flat, owner = [], []
    slots = {i: word_slots(lines[i][1]) for i in idx}
    for i in idx:
        for k, (_, _, norm) in enumerate(slots[i]):
            flat.append(norm)
            owner.append((i, k))
    h_norm, h_time = [], []
    for w, s, e in heard:
        for t in autosync.tokens(w):
            h_norm.append(t)
            h_time.append((s + BIAS, e + BIAS))
    pairs = autosync._match(flat, h_norm)
    got = {i: {} for i in idx}
    for fi, hj in pairs.items():
        i, k = owner[fi]
        got[i][k] = h_time[hj]
    out = {}
    for i in idx:
        sl = slots[i]
        if not sl or len(got[i]) < max(2, MIN_MATCHED * len(sl)) and len(got[i]) < len(sl):
            continue
        t0 = lines[i][0]
        t_next = lines[i + 1][0] if i + 1 < len(lines) else t0 + 10
        ok = {k: v for k, v in got[i].items() if t0 - 1.0 <= v[0] <= t_next + 0.5}
        if len(ok) < max(1, MIN_MATCHED * len(sl)):
            continue
        chars = [None] * len(lines[i][1])
        for k, (s, e) in ok.items():
            off, n, _ = sl[k]
            for c in range(off, off + n):
                chars[c] = (s, e)
        out[i] = chars
    return out


MEASURE_REACH = 12.0   # how far (s) a line may be off and still be found when measuring
MEASURE_MIN_WORDS = 2


def measure_starts(lines: list[tuple[float, str]], measure: set[int], heard, window) -> dict:
    """When each line in `measure` is actually sung: {line index: song time of its first
    word}. Used to notice lyrics timed for another version of the song. A line counts only
    if its first word and at least half of its words were heard, in order."""
    lo, hi = window
    idx = [i for i in sorted(measure)
           if i < len(lines) and lo - MEASURE_REACH <= lines[i][0] <= hi + MEASURE_REACH]
    if not idx or not heard:
        return {}
    flat, owner = [], []
    slots = {i: word_slots(lines[i][1]) for i in idx}
    for i in idx:
        for k, (_, _, norm) in enumerate(slots[i]):
            flat.append(norm)
            owner.append((i, k))
    h_norm, h_time = [], []
    for w, s, e in heard:
        for t in autosync.tokens(w):
            h_norm.append(t)
            h_time.append(s + BIAS)
    pairs = autosync._match(flat, h_norm)
    got = {i: {} for i in idx}
    for fi, hj in pairs.items():
        i, k = owner[fi]
        got[i][k] = h_time[hj]
    out = {}
    for i in idx:
        n = len(slots[i])
        g = got[i]
        if n < MEASURE_MIN_WORDS or 0 not in g or len(g) < max(MEASURE_MIN_WORDS, 0.5 * n):
            continue
        start = g[0]
        if not (lo + 0.3 <= start <= hi - 0.3) or abs(start - lines[i][0]) > MEASURE_REACH:
            continue                     # cut off at the edge of what was heard / too far
        if any(t < start - 0.05 for t in g.values()) or max(g.values()) - start > 15:
            continue                     # words out of order or spread too far: not trusted
        out[i] = start
    return out


class WordLearner(threading.Thread):
    """Background listener. The app calls set_target(); results arrive on .results as
    (song key, {line index: per-char timing}, {line index: heard start})."""

    def __init__(self, watcher):
        super().__init__(daemon=True)
        self.watcher = watcher
        self.results: queue.Queue = queue.Queue()
        self.status = ""
        self._target = None            # (song key, lines, wanted line indexes, lines to measure)
        self._lock = threading.Lock()
        self._buf: list[np.ndarray] = []
        self._marks: list[tuple[int, float, tuple]] = []   # (samples so far, song pos, song key)
        self._total = 0                 # samples received since listening started
        self._held = 0                  # samples currently in _buf
        self._rate = 0
        self._done_until = {}           # song key -> song time already listened to
        self._model = None
        self.enabled = False

    # ---- called by the app ------------------------------------------------
    def set_target(self, key, lines, wanted, measure=()):
        """Song to learn: its lines [(t, text)], the indexes still lacking word timing, and
        the lines whose real start should be measured (to fix badly timed lyrics)."""
        with self._lock:
            self._target = ((key, list(lines), set(wanted), set(measure))
                            if key and (wanted or measure) else None)

    # ---- capture ------------------------------------------------------------
    def _on_audio(self, data, channels):
        mono = np.frombuffer(data, np.float32).reshape(-1, channels).mean(axis=1)
        t = self.watcher.track
        with self._lock:
            self._buf.append(mono.copy())
            self._total += len(mono)
            self._held += len(mono)
            if t is not None:
                self._marks.append((self._total, t.current_position() if t.playing else -1.0, t.key))
            limit = int(KEEP * self._rate)
            while len(self._buf) > 1 and self._held - len(self._buf[0]) >= limit:
                self._held -= len(self._buf.pop(0))
            while self._marks and self._marks[0][0] < self._total - self._held:
                self._marks.pop(0)

    def run(self):
        import pyaudiowpatch as pa
        stream = p = None
        while True:
            time.sleep(1.0)
            with self._lock:
                target = self._target if self.enabled else None
            if target is None:
                if stream is not None:      # nothing to learn: stop listening
                    stream.stop_stream(); stream.close(); p.terminate()
                    stream = p = None
                    with self._lock:
                        self._buf, self._marks, self._total, self._held = [], [], 0, 0
                continue
            if stream is None:
                try:
                    p = pa.PyAudio()
                    wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
                    speaker = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
                    loop = next((d for d in p.get_loopback_device_info_generator()
                                 if speaker["name"] in d["name"]), None)
                    if loop is None:
                        raise RuntimeError("no loopback device")
                    self._rate = int(loop["defaultSampleRate"])
                    ch = max(1, loop["maxInputChannels"])
                    stream = p.open(format=pa.paFloat32, channels=ch, rate=self._rate, input=True,
                                    input_device_index=loop["index"],
                                    frames_per_buffer=self._rate // 20,
                                    stream_callback=lambda d, n, ti, st, ch=ch:
                                    (self._on_audio(d, ch), (None, pa.paContinue))[1])
                except Exception as exc:
                    self.status = f"can't listen: {exc}"
                    if p is not None:
                        p.terminate()
                    stream = p = None
                    time.sleep(10)
                continue
            try:
                self._maybe_learn(target)
            except Exception as exc:          # never take the app down
                self.status = f"error: {exc}"

    # ---- learning -----------------------------------------------------------
    def _piece(self, key, start_pos):
        """Audio since song time start_pos, if at least CHUNK seconds of this song played
        continuously (no pause / skip). Returns (audio 16 kHz, sample->song time map)."""
        with self._lock:
            marks = [m for m in self._marks if m[2] == key and m[1] >= 0]
            if len(marks) < 2:
                return None
            audio = np.concatenate(self._buf) if self._buf else None
            first_sample = self._total - (len(audio) if audio is not None else 0)
        # Continuous stretch at the end: song time advancing like real time.
        run = [marks[-1]]
        for m in reversed(marks[:-1]):
            prev = run[-1]
            expected = prev[1] - (prev[0] - m[0]) / self._rate
            if abs(m[1] - expected) > 0.6:
                break
            run.append(m)
        run.reverse()
        s0 = max(start_pos - OVERLAP, run[0][1])
        if run[-1][1] - s0 < CHUNK:
            return None
        n0 = run[0][0] - int((run[0][1] - s0) * self._rate)
        n1 = run[-1][0]
        a, b = n0 - first_sample, n1 - first_sample
        if a < 0 or audio is None:
            return None
        clip = audio[a:b]
        pos_at = lambda sec: s0 + sec          # continuous: recording seconds -> song time
        return _resample_16k(clip, self._rate), pos_at, run[-1][1]

    def _maybe_learn(self, target):
        key, lines, wanted, measure = target
        done = self._done_until.get(key, -1.0)
        got = self._piece(key, max(done, 0.0))
        if got is None:
            return
        audio, pos_at, end_pos = got
        lo, hi = pos_at(0), end_pos
        needs = [i for i in wanted if lo - 2 <= lines[i][0] <= hi]
        to_measure = [i for i in measure if lo - MEASURE_REACH <= lines[i][0] <= hi + MEASURE_REACH]
        self._done_until[key] = end_pos
        if not (needs or to_measure) or float(np.abs(audio).max()) < 1e-4:
            return                               # instrumental here / already timed / silence
        self.status = "listening…"
        if self._model is None:
            from faster_whisper import WhisperModel
            autosync.MODELS_DIR.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(model_name(), device="cpu", compute_type="int8",
                                       cpu_threads=2, download_root=str(autosync.MODELS_DIR))
        audio = (audio / max(1e-6, float(np.abs(audio).max())) * 0.9).astype(np.float32)
        segments, _ = self._model.transcribe(audio, word_timestamps=True,
                                             condition_on_previous_text=False, beam_size=3)
        heard = []
        for seg in segments:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
                continue
            heard += [(w.word, pos_at(w.start), pos_at(w.end)) for w in (seg.words or [])]
        found = match_lines(lines, set(needs), heard, (lo, hi)) if needs else {}
        starts = measure_starts(lines, set(to_measure), heard, (lo, hi)) if to_measure else {}
        if found or starts:
            with self._lock:
                if self._target and self._target[0] == key:
                    self._target[2].difference_update(found)
                    self._target[3].difference_update(starts)
            self.results.put((key, found, starts))
        self.status = f"learned {len(found)} line(s), measured {len(starts)}"
