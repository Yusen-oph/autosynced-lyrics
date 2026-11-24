"""Runs the Whisper speech model in a separate helper process.

The model needs 200-500 MB while it works. Loaded inside the app, part of that stays
claimed even after the model is dropped (the libraries keep their memory). In a helper
process that exits when it's no longer needed, all of it goes back to Windows.

The helper is this same program started with --whisper-worker (see main.py). It reads
requests on stdin and answers on stdout:

    request:  one JSON line {"model", "threads", "beam", "temperature", "n"}
              then n float32 samples (16 kHz mono)
    answer:   one JSON line per heard phrase {"seg": {...}}, then {"done": duration}
              or {"error": message}
"""
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np

import paths

BELOW_NORMAL = 0x00004000          # the helper yields to everything you're actually using
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---- the helper side ------------------------------------------------------------------
def serve() -> int:
    import autosync
    from faster_whisper import WhisperModel
    models = {}
    inp, out = sys.stdin.buffer, sys.stdout.buffer

    def send(obj):
        out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()

    while True:
        line = inp.readline()
        if not line:
            return 0                                  # the app closed the pipe: done
        try:
            req = json.loads(line)
            raw = inp.read(int(req["n"]) * 4)
            audio = np.frombuffer(raw, np.float32)
            key = (req["model"], int(req.get("threads", 1)))
            if key not in models:
                models.clear()
                autosync.MODELS_DIR.mkdir(parents=True, exist_ok=True)
                models[key] = WhisperModel(req["model"], device="cpu", compute_type="int8",
                                           cpu_threads=key[1], download_root=str(autosync.MODELS_DIR))
            segs, info = models[key].transcribe(
                audio, word_timestamps=True, condition_on_previous_text=False, vad_filter=False,
                beam_size=int(req.get("beam", 1)), temperature=tuple(req.get("temperature", (0.0,))))
            for s in segs:
                send({"seg": {"start": s.start, "end": s.end, "no_speech_prob": s.no_speech_prob,
                              "avg_logprob": s.avg_logprob,
                              "words": [[w.word, w.start, w.end, w.probability] for w in (s.words or [])]}})
            send({"done": info.duration})
        except Exception as exc:                      # tell the app, keep serving
            send({"error": f"{type(exc).__name__}: {exc}"[:300]})


# ---- the app side ---------------------------------------------------------------------
def _command() -> list[str]:
    if paths.FROZEN:
        return [sys.executable, "--whisper-worker"]
    return [sys.executable, str(paths.CODE_DIR / "main.py"), "--whisper-worker"]


class Whisper:
    """A helper process for one model. transcribe() starts it if needed; close() (or
    being idle for idle_after seconds, checked on each use) ends it."""

    def __init__(self, model: str, threads: int = 1, beam: int = 1, temperature=(0.0,),
                 idle_after: float = 60.0):
        self.model, self.threads, self.beam = model, threads, beam
        self.temperature = list(temperature)
        self.idle_after = idle_after
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last = 0.0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self):
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self._proc = subprocess.Popen(_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env, cwd=str(paths.CODE_DIR),
                                      creationflags=NO_WINDOW | BELOW_NORMAL)

    def segments(self, audio: np.ndarray):
        """Yields heard phrases as dicts: start, end, no_speech_prob, avg_logprob,
        words [[word, start, end, probability]]. The last item is {"duration": seconds}."""
        with self._lock:
            if not self.running:
                self._start()
            audio = np.ascontiguousarray(audio, dtype=np.float32)
            head = {"model": self.model, "threads": self.threads, "beam": self.beam,
                    "temperature": self.temperature, "n": int(audio.size)}
            try:
                self._proc.stdin.write((json.dumps(head) + "\n").encode())
                self._proc.stdin.write(audio.tobytes())
                self._proc.stdin.flush()
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        raise RuntimeError("the speech helper stopped")
                    msg = json.loads(line)
                    if "seg" in msg:
                        yield msg["seg"]
                    elif "done" in msg:
                        yield {"duration": msg["done"]}
                        break
                    else:
                        raise RuntimeError(msg.get("error") or "speech helper error")
            except (OSError, ValueError, RuntimeError):
                self._kill()
                raise
            finally:
                self._last = time.monotonic()

    def close_if_idle(self) -> None:
        if self.running and not self._lock.locked() and time.monotonic() - self._last > self.idle_after:
            self.close()

    def close(self) -> None:
        with self._lock:
            self._kill()

    def _kill(self):
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.stdin.close()                      # the helper sees the end and exits by itself
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except OSError:
                pass
