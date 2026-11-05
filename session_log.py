"""Everything a session heard and answered, kept on disk.

One folder per app run in <data folder>/sessions/ (see platform_support.data_dir:
~/Library/Application Support/StealthChat on macOS, %LOCALAPPDATA%\\StealthChat on
Windows, ~/.local/share/StealthChat on Linux):

    audio.mp3        stereo: left = microphone («Я»), right = system audio («Собеседник»)
    transcript.md    the full dialog: **interviewer:** (system audio) / **respondent:** (microphone),
                     each line with the time that person started speaking
    answers.md       every request and the answer to it
    app.log          the app's console output (errors, retries, timings)

Audio is encoded as it arrives, so a crash loses at most a second of it.
"""
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from audio_capture import SAMPLE_RATE
from platform_support import data_dir

SESSIONS_DIR = data_dir() / "sessions"
MP3_KBPS = 48  # plenty for 16 kHz speech; ~21 MB per hour
WRITE_EVERY_S = 1.0
LATENCY_S = 0.5  # how far behind real time the writer stays, so both sources have delivered
MAX_BACKLOG_S = 2.0  # a source running ahead (clock drift) is trimmed to this


class SessionLog:
    def __init__(self):
        self.started = time.time()
        self.dir = SESSIONS_DIR / time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(self.started))
        self.dir.mkdir(parents=True, exist_ok=True)
        self._log = open(self.dir / "app.log", "a", buffering=1, encoding="utf-8")
        self._answers_lock = threading.Lock()
        self.audio = AudioRecorder(self.dir / "audio.mp3")

    def tee_stderr(self):
        """Keep printing to the console, and also into app.log."""
        sys.stderr = _Tee(sys.stderr, self._log)

    def write_transcript(self, text: str):
        tmp = self.dir / "transcript.md.tmp"
        try:
            tmp.write_text(text + "\n", encoding="utf-8")
            tmp.replace(self.dir / "transcript.md")  # atomic: never a half-written file
        except OSError as exc:  # e.g. Windows: another program holds the file open; the next update retries
            print(f"transcript not saved this time: {exc}", file=sys.stderr)

    def add_answer(self, kind: str, prompt: str, answer: str):
        stamp = time.strftime("%H:%M:%S")
        head = f"## [{stamp}] {kind}" + (f": {prompt}" if prompt else "")
        with self._answers_lock, open(self.dir / "answers.md", "a", encoding="utf-8") as f:
            f.write(f"{head}\n\n{answer.strip()}\n\n")

    def close(self):
        self.audio.close()
        if isinstance(sys.stderr, _Tee):
            sys.stderr = sys.stderr.console
        self._log.close()


class AudioRecorder:
    """Mixes the two sources into one stereo MP3, aligned by wall-clock time.

    The sources deliver audio on their own clocks and threads. A writer thread
    takes, once a second, as much audio as real time says should exist from
    each source, padding a source that is silent or stopped with zeros, so the
    two channels stay in step. While listening is paused nothing is written.
    """

    CHANNELS = ("mic", "system")  # left, right

    def __init__(self, path: Path):
        import lameenc

        self._enc = lameenc.Encoder()
        self._enc.set_bit_rate(MP3_KBPS)
        self._enc.set_in_sample_rate(SAMPLE_RATE)
        self._enc.set_channels(2)
        self._enc.set_quality(5)
        self._file = open(path, "ab")
        self._queues = {name: deque() for name in self.CHANNELS}
        self._queued = dict.fromkeys(self.CHANNELS, 0)
        self._lock = threading.Lock()
        self._origin = None  # wall-clock time that sample 0 of the current run corresponds to
        self._written = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mp3", daemon=True)
        self._thread.start()

    def add(self, channel: str, chunk: np.ndarray):
        with self._lock:
            if self._origin is None:
                return
            self._queues[channel].append(chunk)
            self._queued[channel] += len(chunk)

    def resume(self):
        with self._lock:
            self._origin = time.time() - self._written / SAMPLE_RATE  # continue where the file ends

    def pause(self):
        self._write(final=True)
        with self._lock:
            self._origin = None
            for name in self.CHANNELS:
                self._queues[name].clear()
                self._queued[name] = 0

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)
        self.pause()
        self._file.write(self._enc.flush())
        self._file.close()

    def _run(self):
        while not self._stop.wait(WRITE_EVERY_S):
            try:
                self._write()
            except Exception as exc:  # never take the app down over the recording
                print(f"recording error: {exc}", file=sys.stderr)

    def _write(self, final=False):
        with self._lock:
            if self._origin is None:
                return
            target = int((time.time() - self._origin - (0 if final else LATENCY_S)) * SAMPLE_RATE)
            need = target - self._written
            if need <= 0:
                return
            channels = [self._take(name, need) for name in self.CHANNELS]
            self._written = target
        pcm = (np.clip(np.stack(channels, axis=1), -1, 1) * 32767).astype("<i2")
        self._file.write(self._enc.encode(pcm.tobytes()))
        self._file.flush()

    def _take(self, name: str, n: int) -> np.ndarray:
        q = self._queues[name]
        excess = self._queued[name] - n - int(MAX_BACKLOG_S * SAMPLE_RATE)
        while excess > 0 and q:  # this source's clock runs fast: drop the oldest audio
            dropped = q.popleft()
            self._queued[name] -= len(dropped)
            excess -= len(dropped)
        out, got = [], 0
        while q and got < n:
            chunk = q.popleft()
            if got + len(chunk) > n:
                q.appendleft(chunk[n - got:])
                chunk = chunk[:n - got]
            out.append(chunk)
            got += len(chunk)
        self._queued[name] -= got
        if got < n:  # silent or stopped source
            out.append(np.zeros(n - got, np.float32))
        return np.concatenate(out)


class _Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, s):
        if self.console is not None:  # None under pythonw.exe (Windows, no console window)
            self.console.write(s)
        try:
            self.log.write(s)
        except ValueError:  # log already closed during shutdown
            pass
        return len(s)

    def flush(self):
        if self.console is not None:
            self.console.flush()
