"""AI auto-sync: record what the music app plays, transcribe it with Whisper, line the lyrics up.

1. Recorder captures system audio (WASAPI loopback) and notes the player's playback position
   every half second, so recording time can be mapped to song time even across pauses.
2. faster-whisper transcribes the recording with per-word timestamps.
3. align() matches the lyric words to the heard words (fuzzy, in order) and gives each
   line a start time. Lines it couldn't hear are estimated and flagged as guesses.
"""
import os
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

import paths

MODELS_DIR = paths.MODELS_DIR
MODEL_SIZES = {  # name -> approx. download size
    "base": "~150 MB, fastest, least accurate",
    "small": "~480 MB, good balance",
    "medium": "~1.5 GB, best, slow on CPU",
}


# ---- 1. recording ------------------------------------------------------------
class Recorder(threading.Thread):
    def __init__(self, watcher, max_seconds: float = 15 * 60):
        super().__init__(daemon=True)
        self.watcher = watcher
        self.max_seconds = max_seconds
        self.stop_flag = threading.Event()
        self.rate = 0
        self.chunks: list[np.ndarray] = []
        self.checkpoints: list[tuple[int, float]] = []   # (samples so far, song position)
        self.error: str | None = None
        self.stop_reason = ""

    @property
    def seconds(self) -> float:
        return sum(len(c) for c in self.chunks) / self.rate if self.rate else 0.0

    def stop(self, reason: str = "stopped"):
        self.stop_reason = self.stop_reason or reason
        self.stop_flag.set()

    def run(self):
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            speaker = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            loop = next((d for d in p.get_loopback_device_info_generator()
                         if speaker["name"] in d["name"]), None)
            if loop is None:
                raise RuntimeError("Couldn't find a loopback device for your speakers.")
            self.rate = int(loop["defaultSampleRate"])
            channels = max(1, loop["maxInputChannels"])
            start_key = self.watcher.track.key if self.watcher.track else None
            total = 0

            def on_audio(in_data, frame_count, time_info, status):
                # Runs on the audio thread for every small block: never drops samples.
                nonlocal total
                mono = np.frombuffer(in_data, np.float32).reshape(-1, channels).mean(axis=1)
                self.chunks.append(mono.copy())
                total += len(mono)
                t = self.watcher.track
                if t is not None:
                    self.checkpoints.append((total, t.current_position()))
                return (None, pa.paContinue)

            stream = p.open(format=pa.paFloat32, channels=channels, rate=self.rate, input=True,
                            input_device_index=loop["index"], frames_per_buffer=self.rate // 20,
                            stream_callback=on_audio)
            last_wall, last_pos = time.monotonic(), None
            try:
                while not self.stop_flag.wait(0.25):
                    t = self.watcher.track
                    now = time.monotonic()
                    pos = t.current_position() if t else None
                    # A jump in position means a seek: lyrics would be heard out of order.
                    if pos is not None and last_pos is not None and t.playing \
                            and abs(pos - (last_pos + (now - last_wall))) > 2.0:
                        self.stop("you skipped within the song, stopped there")
                        break
                    last_wall, last_pos = now, pos
                    if t is None or t.key != start_key:
                        self.stop("song changed")
                    elif t.duration and t.current_position() >= t.duration - 0.5:
                        self.stop("song finished")
                    elif total / self.rate > self.max_seconds:
                        self.stop("time limit")
            finally:
                stream.stop_stream()
                stream.close()
        except Exception as exc:
            self.error = str(exc)
            self.stop("error")
        finally:
            p.terminate()

    def song_time(self, recording_seconds: float) -> float:
        """Map a time in the recording to the song position heard at that moment."""
        n = recording_seconds * self.rate
        for samples, pos in self.checkpoints:
            if samples >= n:
                return pos - (samples - n) / self.rate
        if self.checkpoints:
            samples, pos = self.checkpoints[-1]
            return pos + (n - samples) / self.rate
        return recording_seconds

    def audio_16k(self) -> np.ndarray:
        """Mono float32 audio at 16 kHz, which is what Whisper expects."""
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(1600, np.float32)
        if self.rate == 16000:
            return audio.astype(np.float32)
        if self.rate % 16000 == 0:  # e.g. 48 kHz: average groups of samples (simple low-pass)
            k = self.rate // 16000
            audio = audio[: len(audio) // k * k].reshape(-1, k).mean(axis=1)
            return audio.astype(np.float32)
        n = int(len(audio) * 16000 / self.rate)
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


    def snippet_16k(self, song_start: float, song_end: float) -> np.ndarray | None:
        """The recorded audio between two song positions (None if it wasn't recorded)."""
        audio = self.audio_16k()

        def at(song_t):
            for samples, pos in self.checkpoints:
                if pos >= song_t:
                    return max(0.0, samples - (pos - song_t) * self.rate) / self.rate
            return None
        s0, s1 = at(song_start), at(song_end)
        if s0 is None:
            return None
        s1 = s1 if s1 is not None else len(audio) / 16000
        piece = audio[int(s0 * 16000):int(s1 * 16000)]
        return piece if len(piece) > 1600 else None


# ---- slowed-down replay (same pitch) ---------------------------------------
def time_stretch(x: np.ndarray, speed: float, sr: int = 16000) -> np.ndarray:
    """Play `speed` times as fast (0.75 = slower) without changing pitch (WSOLA)."""
    frame = int(0.05 * sr)
    hop_out = frame // 2
    hop_in = hop_out * speed
    tol = int(0.012 * sr)
    window = np.hanning(frame).astype(np.float32)
    x = np.concatenate([np.zeros(tol, np.float32), x.astype(np.float32), np.zeros(frame + tol, np.float32)])
    out = np.zeros(int(len(x) / speed) + frame, np.float32)
    norm = np.zeros_like(out)
    pos_in, pos_out, prev = float(tol), 0, None
    while int(pos_in) + frame + tol < len(x) and pos_out + frame < len(out):
        target = int(pos_in)
        if prev is None:
            start = target
        else:
            natural = x[prev + hop_out: prev + hop_out + frame]   # how the last piece continues
            region = x[target - tol: target + tol + frame]
            start = target - tol + int(np.argmax(np.correlate(region, natural, "valid")))
        out[pos_out:pos_out + frame] += x[start:start + frame] * window
        norm[pos_out:pos_out + frame] += window
        prev, pos_out, pos_in = start, pos_out + hop_out, pos_in + hop_in
    out = out[:pos_out] / np.maximum(norm[:pos_out], 1e-3)
    return out


def play_audio(x: np.ndarray, sr: int = 16000) -> None:
    """Play mono audio on the default speakers (blocking; call from a thread)."""
    import pyaudiowpatch as pa
    x = (x / max(1e-6, float(np.abs(x).max())) * 0.8).astype(np.float32)
    p = pa.PyAudio()
    try:
        stream = p.open(format=pa.paFloat32, channels=1, rate=sr, output=True)
        try:
            stream.write(x.tobytes())
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        p.terminate()


# ---- 2. transcription ------------------------------------------------------
_model_cache = {}
# Whisper would use every core by default (half the CPU or more on a 12-thread PC). A few
# threads are plenty: the editor's auto-sync is a bit slower but the PC stays responsive.
CPU_THREADS = max(2, min(4, (os.cpu_count() or 4) // 4))


def transcribe(audio: np.ndarray, model_size: str, progress=lambda f, msg: None):
    """Returns [(word, start_s, end_s, confidence 0-1, phrase number)] in recording time."""
    from faster_whisper import WhisperModel

    if model_size not in _model_cache:
        progress(0.0, f"Loading the {model_size} model (downloads once, {MODEL_SIZES[model_size].split(',')[0]})…")
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        _model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8",
                                                cpu_threads=CPU_THREADS, download_root=str(MODELS_DIR))
    model = _model_cache[model_size]
    try:
        return _transcribe(model, audio, progress)
    finally:
        release_models()                # the model takes 200-500 MB: don't keep it around


def release_models() -> None:
    _model_cache.clear()
    import gc
    gc.collect()


def _transcribe(model, audio, progress):
    """Run the model; returns the heard words (see transcribe)."""

    # Loopback audio is often quiet; normalising helped recognition in testing.
    audio = (audio / max(1e-6, float(np.abs(audio).max())) * 0.9).astype(np.float32)
    progress(0.0, "Listening to the recording…")
    segments, info = model.transcribe(audio, word_timestamps=True, condition_on_previous_text=False,
                                      vad_filter=False, beam_size=3,
                                      temperature=(0.0, 0.4, 0.8))   # default retries up to 5 times
    words = []
    for n, seg in enumerate(segments):
        progress(min(0.99, seg.end / max(1.0, info.duration)), "Listening to the recording…")
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
            continue  # Whisper "hearing" words in an instrumental part
        words += [(w.word, w.start, w.end, w.probability, n) for w in (seg.words or [])]
    return words


# ---- draft lyrics from what was heard ----------------------------------------
UNSURE_BELOW = 0.45     # word confidence under this gets marked [word?]
_UNSURE = re.compile(r"\[([^\[\]]+)\?\]")


def draft_lyrics(words, to_song_time) -> list[tuple[float, str]]:
    """Group heard words into timed lyric lines; unclear or censored words become [word?]."""
    lines, cur, cur_start, last_end, last_seg = [], [], None, None, None

    def flush():
        if cur:
            text = " ".join(cur).strip().rstrip(".,;:")
            lines.append((max(0.0, to_song_time(cur_start) + BIAS_SECONDS), text))

    for w, start, end, prob, seg in words:
        w = w.strip()
        if not w:
            continue
        new_line = (
            cur and (seg != last_seg                                   # Whisper's phrase ended
                     or start - last_end > 0.7                         # a pause in the singing
                     or len(cur) >= 12                                 # too long for one line
                     or (len(cur) >= 7 and cur[-1][-1:] in ",.!?")))   # natural break
        if new_line:
            flush()
            cur, cur_start = [], None
        if cur_start is None:
            cur_start = start
        if "*" in w or prob < UNSURE_BELOW:
            core = w.strip(".,!?;:")
            w = f"[{core}?]" + w[len(w.rstrip('.,!?;:')):] if core else w
        cur.append(w)
        last_end, last_seg = end, seg
    flush()
    return lines


def count_unsure(text: str) -> int:
    return len(_UNSURE.findall(text))


# ---- 3. alignment ----------------------------------------------------------
_WIDE = r"⺀-鿿가-힯぀-ヿ＀-￯"
_TOKEN = re.compile(rf"[{_WIDE}]|(?:(?![{_WIDE}])\w)+")
WORD_SECONDS = 0.32  # rough sung-word length, used to back-date a line start
# Measured against published synced lyrics: Whisper-derived starts landed ~0.3-0.6 s early.
BIAS_SECONDS = 0.3


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower().replace("'", "").replace("’", "")
    return _TOKEN.findall(text)


def _similar(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if "*" in b:  # censored heard word: "f***" matches a lyric word starting with "f"
        prefix = b.split("*")[0]
        return 0.8 if prefix and a.startswith(prefix) and len(a) > len(prefix) else 0.0
    if len(a) == 1 or len(b) == 1 or abs(len(a) - len(b)) > max(len(a), len(b)) / 2:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _match(lyric: list[str], heard: list[str]) -> dict[int, int]:
    """Order-preserving fuzzy alignment (weighted LCS). Returns lyric index -> heard index."""
    n, m = len(lyric), len(heard)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        a, row, prev = lyric[i - 1], score[i], score[i - 1]
        for j in range(1, m + 1):
            best = max(prev[j], row[j - 1])
            s = _similar(a, heard[j - 1])
            if s >= 0.7:
                best = max(best, prev[j - 1] + s)
            row[j] = best
    pairs, i, j = {}, n, m
    while i > 0 and j > 0:
        s = _similar(lyric[i - 1], heard[j - 1])
        if s >= 0.7 and abs(score[i][j] - (score[i - 1][j - 1] + s)) < 1e-4:
            pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif score[i - 1][j] >= score[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return pairs


def align(lines: list[str], words, to_song_time) -> tuple[list[float | None], list[bool]]:
    """Start time per lyric line (None for blank lines) and whether each one is a guess."""
    line_tokens = [tokens(l) for l in lines]
    flat, owner, offset = [], [], []
    for li, toks in enumerate(line_tokens):
        for k, t in enumerate(toks):
            flat.append(t)
            owner.append(li)
            offset.append(k)
    heard, heard_t = [], []
    for w, start, *_ in words:
        if "*" in w:   # censored by Whisper, e.g. "f***": keep it whole so it can still match
            toks = [re.sub(r"[^\w*]", "", w.lower())]
        else:
            toks = tokens(w)
        for t in toks:
            heard.append(t)
            heard_t.append(to_song_time(start))
    pairs = _match(flat, heard)

    times: list[float | None] = [None] * len(lines)
    guessed = [False] * len(lines)
    hits = {li: [] for li in range(len(lines))}
    for fi, hj in pairs.items():
        hits[owner[fi]].append((offset[fi], heard_t[hj]))
    for li, toks in enumerate(line_tokens):
        if not toks:
            continue
        h = hits[li]
        if len(h) >= max(1, round(len(toks) * 0.25)):
            h.sort()
            if h[0][0] == 0:
                times[li] = h[0][1] + BIAS_SECONDS
            else:
                times[li] = float(np.median([t - k * WORD_SECONDS for k, t in h])) + BIAS_SECONDS

    # Fill unheard lines in between known ones, spread by word count.
    lyric_lines = [li for li, toks in enumerate(line_tokens) if toks]
    known = [li for li in lyric_lines if times[li] is not None]
    for li in lyric_lines:
        if times[li] is not None:
            continue
        guessed[li] = True
        before = max((k for k in known if k < li), default=None)
        after = min((k for k in known if k > li), default=None)
        if before is not None and after is not None:
            span = [x for x in lyric_lines if before <= x < after]
            weights = np.cumsum([0] + [len(line_tokens[x]) for x in span])
            frac = weights[span.index(li)] / weights[-1]
            times[li] = times[before] + frac * (times[after] - times[before])
        elif before is not None:
            gap = sum(len(line_tokens[x]) for x in lyric_lines if before <= x < li)
            times[li] = times[before] + gap * WORD_SECONDS * 1.5
        elif after is not None:
            gap = sum(len(line_tokens[x]) for x in lyric_lines if li <= x < after)
            times[li] = max(0.0, times[after] - gap * WORD_SECONDS * 1.5)
        else:
            times[li] = None

    # Keep times strictly increasing; anything out of order becomes a guess.
    last = -1.0
    for li in lyric_lines:
        t = times[li]
        if t is None:
            continue
        if t <= last:
            times[li] = last + 0.3
            guessed[li] = True
        times[li] = max(0.0, times[li])
        last = times[li]
    return times, guessed
