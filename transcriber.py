"""Continuous dialog transcription: audio sources → speech segments → GigaChat text.

Each source (microphone = "Я", system audio = "Собеседник") is cut into
segments at pauses in speech, so a phrase is rarely split mid-word. Segments
are uploaded and transcribed in parallel in the background; the transcript is
kept in the order the speech happened. When an answer is requested, segments
that are still being transcribed are waited for (about a second): attaching
the raw audio to the answer request instead was tried, but GigaChat ignores
audio in a request that also carries a system prompt and a transcript.

Answers get the last CONTEXT_CHARS of the dialog word for word. Older parts are
folded into a running summary in the background, so a long call keeps its
early questions, names and numbers without the request growing without bound.
"""
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, Signal

from ai_client import GigaChatClient, wav_bytes
from audio_capture import SAMPLE_RATE, MicCapture, SystemAudioCapture

ME, THEM = "Я", "Собеседник"
MD_ROLES = {THEM: "interviewer", ME: "respondent"}  # names in the session's transcript.md
MD_PAUSE_S = 3.0  # a pause this long starts a new line in transcript.md...
MD_LINE_MAX_S = 60.0  # ...and so does a line that already covers this much time

FRAME = SAMPLE_RATE // 50  # 20 ms analysis frames
MIN_SEGMENT_S = 1.25  # don't cut at a pause before this much audio
SOFT_MAX_S = 4.5  # past this, a short pause (comma, breath) is enough to cut
SHORT_PAUSE_S = 0.25  # longer than the closure inside a consonant cluster ("кт"), so it is a real gap
MAX_SEGMENT_S = 6.0  # cut even without a pause (music, monologue), at the quietest moment of...
CUT_SEARCH_S = 2.5  # ...this last stretch, so a word is rarely split in two
CUT_GAP_S = 0.2  # quietness is judged over windows this long: a gap between words, not a consonant
PAUSE_S = 0.5  # silence that ends a phrase
PREROLL_S = 0.3  # audio kept before speech starts, so the first syllable isn't lost
MIN_VOICED_S = 0.5  # segments with less speech than this are dropped
ECHO_HOLD_S = 0.5  # mic frames don't count as speech this long after system audio was voiced
NOISE_TAGS = re.compile(r"\[[^\]]*\]|\((?:музыка|шум|смех|аплодисменты|звук|помехи|тишина)[^)]*\)", re.I)
MAX_WORDS_PER_S = 5  # more words than this per second of speech = hallucinated text, dropped
SOURCE_RETRY_S = 20  # a source that failed to start is retried this often
SOURCE_STALL_S = 5.0  # a running source with no buffers for this long is restarted
STATS_EVERY_S = 300  # how often audio levels per source go to the log
CONTEXT_CHARS = 80000  # recent dialog sent word for word with each answer (~40 min of talk)
SUMMARY_KEEP_CHARS = CONTEXT_CHARS // 2  # when folding into the summary, this much recent text stays verbatim
SUMMARY_PROMPT = (
    "Ниже — краткое содержание начала разговора (может быть пустым) и следующая часть его расшифровки. "
    "Составь новое краткое содержание всего этого: кто что спрашивал и отвечал, все заданные вопросы, "
    "задачи, факты, имена, цифры, термины и договорённости. Пиши сжато, списком, по порядку, не больше "
    "6000 символов. Ничего не выдумывай.\n\nКраткое содержание:\n{summary}\n\nРасшифровка:\n{text}"
)


@dataclass(eq=False)  # identity comparison: fields hold numpy arrays
class Segment:
    speaker: str
    started: float  # wall-clock time of the first sample
    samples: np.ndarray | None  # freed once transcribed
    voiced_s: float = 0.0  # seconds of detected speech
    speech_at: float = 0.0  # wall-clock time the speech itself starts (after the pre-roll)
    ended: float = 0.0  # wall-clock time of the last sample
    text: str | None = None  # None until transcribed
    upload: Future | None = None  # -> file id
    done: Future | None = None  # transcription finished


class Segmenter:
    """Energy-based voice activity detection with an adaptive noise floor.

    ``echo_of`` is another segmenter (system audio): while it hears sound, this
    one ignores its own input, so laptop speakers leaking into the mic don't
    produce a duplicate of the other person's words as "Я".
    """

    def __init__(self, speaker: str, on_segment, echo_of: "Segmenter | None" = None):
        self.speaker, self.on_segment, self.echo_of = speaker, on_segment, echo_of
        self.last_voiced = 0.0  # wall-clock time of the last voiced frame
        self._lock = threading.Lock()
        self._pending = np.zeros(0, np.float32)  # samples not yet analysed (< one frame)
        self._frames: list[np.ndarray] = []
        self._levels: list[float] = []  # RMS per frame, to find a quiet place for a forced cut
        self._voiced_flags: list[bool] = []
        self._voiced = 0  # voiced frames in the current segment
        self._silence = 0  # trailing silent frames
        self._started = 0.0
        self._noise = 0.003
        self.level = 0.0  # last frame RMS, for the UI meter

    def feed(self, chunk: np.ndarray):
        with self._lock:
            data = np.concatenate([self._pending, chunk])
            n = len(data) // FRAME
            self._pending = data[n * FRAME:]
            for frame in data[:n * FRAME].reshape(n, FRAME):
                self._frame(frame)

    def _frame(self, frame: np.ndarray):
        rms = float(np.sqrt(np.mean(frame * frame)))
        self.level = rms
        voiced = rms > max(0.006, self._noise * 3)
        if not voiced:  # track the background level only while nobody is talking
            self._noise = 0.995 * self._noise + 0.005 * rms
        now = time.time()
        if voiced and self.echo_of is not None and now - self.echo_of.last_voiced < ECHO_HOLD_S:
            voiced = False
        if voiced:
            self.last_voiced = now
        if not self._frames:
            self._started = time.time() - FRAME / SAMPLE_RATE
        self._frames.append(frame)
        self._levels.append(rms)
        self._voiced_flags.append(voiced)
        if voiced:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1

        seconds = len(self._frames) * FRAME / SAMPLE_RATE
        if self._voiced == 0:
            keep = int(PREROLL_S * SAMPLE_RATE / FRAME)  # nothing said yet: keep only a short pre-roll
            if len(self._frames) > keep:
                drop = len(self._frames) - keep
                del self._frames[:drop], self._levels[:drop], self._voiced_flags[:drop]
                self._started += drop * FRAME / SAMPLE_RATE
        elif seconds >= MIN_SEGMENT_S and self._silence * FRAME / SAMPLE_RATE >= PAUSE_S:
            self._emit()
        elif seconds >= SOFT_MAX_S and self._silence * FRAME / SAMPLE_RATE >= SHORT_PAUSE_S:
            self._emit()
        elif seconds >= MAX_SEGMENT_S:
            search = int(CUT_SEARCH_S * SAMPLE_RATE / FRAME)
            gap = int(CUT_GAP_S * SAMPLE_RATE / FRAME)
            tail = np.asarray(self._levels[-search:])
            window = np.convolve(tail, np.ones(gap) / gap, mode="valid")  # mean level of each gap-long window
            middle = int(np.argmin(window)) + gap // 2  # cut in the middle of the quietest window
            self._emit(len(self._frames) - len(tail) + middle)

    def _emit(self, upto: int | None = None):
        """Send frames[:upto] as a segment; the rest starts the next one."""
        upto = len(self._frames) if upto is None else upto
        frames, flags, started = self._frames[:upto], self._voiced_flags[:upto], self._started
        self._frames, self._levels = self._frames[upto:], self._levels[upto:]
        self._voiced_flags = self._voiced_flags[upto:]
        self._voiced = sum(self._voiced_flags)
        self._silence = next((i for i, v in enumerate(reversed(self._voiced_flags)) if v), len(self._voiced_flags))
        self._started = started + upto * FRAME / SAMPLE_RATE
        voiced_s = sum(flags) * FRAME / SAMPLE_RATE
        if frames and voiced_s >= MIN_VOICED_S:
            speech_at = started + flags.index(True) * FRAME / SAMPLE_RATE
            ended = started + len(frames) * FRAME / SAMPLE_RATE
            self.on_segment(Segment(self.speaker, started, np.concatenate(frames), voiced_s, speech_at, ended))

    def flush(self):
        """Close the current segment now (used right before an answer is requested)."""
        with self._lock:
            self._emit()


class _Signals(QObject):
    updated = Signal()  # transcript changed
    status = Signal(str)  # human-readable capture status / errors


class DialogListener:
    def __init__(self, client: GigaChatClient, session=None):
        self.client = client
        self.session = session  # SessionLog: audio + full transcript on disk, or None
        self.signals = _Signals()
        self.segments: list[Segment] = []  # what the window shows (Clear empties it)
        self._history: list[Segment] = []  # everything this session, for the transcript file
        self.summary = ""  # folded-up earlier part of the dialog
        self._summary_until = 0.0  # segments that started before this are in the summary
        self._summarizing = False
        self._lock = threading.Lock()
        # Separate pools, so an upload needed for an answer never waits behind transcriptions.
        # The client queues requests to the plan's concurrency limit; workers only keep the queue fed.
        self._uploads = ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload")
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stt")
        self._stored: list[tuple[Segment, str]] = []  # files in GigaChat storage, deleted when no longer needed
        self.system = Segmenter(THEM, self._add)
        self.mic = Segmenter(ME, self._add, echo_of=self.system)
        self._sources = []
        self.running = False
        self._generation = 0  # bumped on every start, so a previous supervisor thread winds down
        self._last_audio = {"system": 0.0, "mic": 0.0}  # wall-clock time of each source's last buffer
        self._stats = {"system": [0.0, 0.0], "mic": [0.0, 0.0]}  # seconds received, peak level
        self._status, self._error = "", False

    # ---------- capture ----------
    def start(self):
        """Start listening. Sources come up in the background and are kept running by _supervise."""
        if self.running:
            return
        self.running = True
        self._generation += 1
        if self.session:
            self.session.audio.resume()
        self._set_status("starting…")
        threading.Thread(target=self._supervise, args=(self._generation,), name="sources", daemon=True).start()

    def _supervise(self, generation: int):
        """Keep every source running while listening is on.

        A source that fails to start is retried every SOURCE_RETRY_S (ScreenCaptureKit
        can stop answering until its daemon recovers). A running source that stops
        delivering audio is restarted: both sources send buffers continuously, even
        in silence, so a gap means the stream died without saying so (system audio
        after a capture-daemon hiccup, the mic when the audio device changes).
        """
        active: dict[str, object] = {}
        retry_at: dict[str, float] = {}
        problems: dict[str, str] = {}
        report_at = time.time() + STATS_EVERY_S
        while self.running and self._generation == generation:
            now = time.time()
            for name, cls, segmenter, channel in self._source_specs():
                src = active.get(channel)
                if src is not None and now - self._last_audio[channel] > SOURCE_STALL_S:
                    print(f"listener: {name} sent no audio for {SOURCE_STALL_S:.0f}s; restarting it", file=sys.stderr)
                    with self._lock:
                        if src in self._sources:
                            self._sources.remove(src)
                    _stop_quietly(src)
                    del active[channel]
                    retry_at[channel] = now
                if channel in active or now < retry_at.get(channel, 0):
                    continue
                try:
                    src = cls(lambda chunk, seg=segmenter, ch=channel: self._on_audio(seg, ch, chunk))
                    self._last_audio[channel] = time.time()
                    src.start()
                except Exception as exc:  # one missing permission shouldn't disable the other source
                    problems[channel] = f"{name}: {exc}"
                    retry_at[channel] = time.time() + SOURCE_RETRY_S
                    continue
                with self._lock:
                    current = self.running and self._generation == generation
                    if current:
                        self._sources.append(src)
                if not current:  # stopped while this source was starting
                    _stop_quietly(src)
                    return
                self._last_audio[channel] = time.time()
                active[channel] = src
                problems.pop(channel, None)
            status = "listening" if not problems else \
                "⚠ " + "; ".join(problems.values()) + f" — retrying every {SOURCE_RETRY_S}s"
            if status != self._status:
                self._set_status(status, error=bool(problems))
            if time.time() >= report_at:
                self._report_levels()
                report_at = time.time() + STATS_EVERY_S
            time.sleep(0.5)

    def _source_specs(self):
        return (("system audio", SystemAudioCapture, self.system, "system"), ("mic", MicCapture, self.mic, "mic"))

    def _report_levels(self):
        """Log how much audio each source delivered and how loud it was: tells "quiet" from "dead"."""
        parts = []
        for channel in ("system", "mic"):
            seconds, peak = self._stats[channel]
            self._stats[channel] = [0.0, 0.0]
            parts.append(f"{channel} {seconds:.0f}s received, peak {peak:.3f}" + (" (silent)" if peak < 0.002 else ""))
        print(f"audio last {STATS_EVERY_S // 60} min: " + " | ".join(parts), file=sys.stderr)

    def _set_status(self, status: str, error: bool = False):
        self._status, self._error = status, error
        print(f"listener: {status}", file=sys.stderr)
        self.signals.status.emit(status)

    def stop(self):
        with self._lock:
            self.running = False
            sources, self._sources = self._sources, []
        for src in sources:
            _stop_quietly(src)
        self.mic.flush()
        self.system.flush()
        if self.session:
            self.session.audio.pause()
        self._set_status("paused")

    def shutdown(self, timeout: float = 30.0):
        """Clean stop: capture off, the phrases still in flight transcribed and saved, then exit."""
        if self.running:
            self.stop()
        with self._lock:
            pending = [s.done for s in self._history if s.text is None and s.done is not None]
        if pending:
            print(f"listener: saving — transcribing the last {len(pending)} phrases", file=sys.stderr)
            started = time.time()
            wait(pending, timeout)
            left = sum(not f.done() for f in pending)
            print(f"listener: saved in {time.time() - started:.1f}s" + (f", {left} phrases timed out" if left else ""),
                  file=sys.stderr)
        self._write_transcript()
        self._uploads.shutdown(wait=False, cancel_futures=True)
        self._pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.time() + 3  # tidy GigaChat storage if it's quick; leftovers are harmless
        for _, file_id in self._stored:
            if time.time() > deadline:
                break
            self.client.delete_file(file_id, wait=0.5)

    def _on_audio(self, segmenter: Segmenter, channel: str, chunk: np.ndarray):
        self._last_audio[channel] = time.time()
        stats = self._stats[channel]
        stats[0] += len(chunk) / SAMPLE_RATE
        if len(chunk):
            stats[1] = max(stats[1], float(np.abs(chunk).max()))
        segmenter.feed(chunk)
        if self.session:
            self.session.audio.add(channel, chunk)

    # ---------- transcription ----------
    def _add(self, seg: Segment):
        with self._lock:
            self.segments.append(seg)
            self.segments.sort(key=lambda s: s.started)
            self._history.append(seg)
        seg.upload = self._uploads.submit(self._upload, seg)
        seg.done = self._pool.submit(self._transcribe, seg)

    def _upload(self, seg: Segment) -> str:
        file_id = self.client.upload(wav_bytes(seg.samples, SAMPLE_RATE), "speech.wav", "audio/wav")
        with self._lock:
            self._stored.append((seg, file_id))
        return file_id

    def _transcribe(self, seg: Segment):
        try:
            text = self.client.transcribe_file(seg.upload.result())
            for _ in range(2):  # clear speech but no text: an occasional GigaChat fluke, so retry with a fresh upload
                if text or seg.voiced_s < 1.5:
                    break
                text = self.client.transcribe_file(self._upload(seg))
            if self._error and self._status.startswith("⚠ transcription"):  # recovered
                self._set_status("listening")
        except Exception as exc:
            text = ""
            self._set_status(f"⚠ transcription error: {exc}", error=True)
        text = " ".join(NOISE_TAGS.sub(" ", text).split())  # "[Звук помех]", "(музыка)": not speech
        if len(text.split()) > MAX_WORDS_PER_S * seg.voiced_s + 3:
            text = ""
        seg.text = text
        seg.samples = None  # uploaded and transcribed: the audio lives on only in the session MP3
        self.signals.updated.emit()
        self._write_transcript()
        self._maybe_summarize()
        self._cleanup()

    def _write_transcript(self):
        if self.session:
            with self._lock:
                history = sorted(self._history, key=lambda s: s.started)
            self.session.write_transcript(_format_markdown(history, self.session.started))

    def _cleanup(self, keep_s: float = 300):
        """Delete transcribed audio from storage once no answer request can still be using it."""
        cutoff = time.time() - keep_s
        with self._lock:
            old = [(s, f) for s, f in self._stored if s.text is not None and s.started < cutoff]
            self._stored = [item for item in self._stored if item not in old]
        for _, file_id in old:
            self.client.delete_file(file_id)

    # ---------- reading ----------
    def flush(self):
        self.mic.flush()
        self.system.flush()

    def transcript(self) -> str:
        """The whole transcript shown in the window, oldest first."""
        with self._lock:
            return _format(self.segments)

    def context(self) -> str:
        """What an answer request gets: the summary of earlier talk plus the recent dialog verbatim."""
        with self._lock:
            recent = _format([s for s in self.segments if s.started >= self._summary_until])
            summary = self.summary
        if len(recent) > CONTEXT_CHARS:  # a summary is on its way; until then, the newest part only
            recent = recent[recent.find("\n", len(recent) - CONTEXT_CHARS) + 1:]
        if not summary:
            return recent
        return f"Краткое содержание начала разговора:\n{summary}\n\nДальше дословно:\n{recent}"

    def _maybe_summarize(self, force=False):
        """Fold the oldest part of the dialog into the summary once the verbatim part gets too long."""
        with self._lock:
            if self._summarizing:
                return
            recent = [s for s in self.segments if s.started >= self._summary_until]
            if not force and len(_format(recent)) <= CONTEXT_CHARS:
                return
            keep, size = len(recent), 0  # recent[keep:] stays verbatim
            while keep > 0 and size + len(recent[keep - 1].text or "") < SUMMARY_KEEP_CHARS:
                keep -= 1
                size += len(recent[keep].text or "") + 30
            pending = next((i for i, s in enumerate(recent[:keep]) if s.text is None), keep)
            keep = pending  # a phrase still being transcribed must not land in the folded part
            fold = recent[:keep]
            if not fold:
                return
            self._summarizing = True
            until = recent[keep].started if keep < len(recent) else fold[-1].started + 1e-3
        self._pool.submit(self._summarize, fold, until)

    def _summarize(self, fold: list[Segment], until: float):
        try:
            summary = self.client.complete([{"role": "user", "content": SUMMARY_PROMPT.format(
                summary=self.summary or "(пусто)", text=_format(fold))}], temperature=0.1)
            with self._lock:
                self.summary, self._summary_until = summary.strip(), until
            print(f"context: folded {len(fold)} segments into a {len(self.summary)}-char summary", file=sys.stderr)
        except Exception as exc:
            print(f"context: summary failed ({exc}); answers use the newest part only", file=sys.stderr)
        finally:
            self._summarizing = False

    def compact_now(self, timeout: float = 30.0):
        """Fold everything but the newest dialog into the summary right away (when a request is too big)."""
        self._maybe_summarize(force=True)
        deadline = time.time() + timeout
        while self._summarizing and time.time() < deadline:
            time.sleep(0.1)

    def wait_pending(self, timeout: float = 10.0):
        """Block until every segment captured so far is transcribed (or the timeout passes)."""
        with self._lock:
            pending = [s.done for s in self.segments if s.text is None and s.done is not None]
        wait(pending, timeout)

    def clear(self):
        """Empty the window's transcript and the answer context (the session file keeps everything)."""
        with self._lock:
            self.segments = []
            self.summary, self._summary_until = "", 0.0
        self.signals.updated.emit()


def _format(segments: list[Segment]) -> str:
    """Transcribed lines, oldest first; consecutive lines of one speaker are merged."""
    lines = []
    for s in segments:
        if not s.text:
            continue
        stamp = time.strftime("%H:%M:%S", time.localtime(s.started))
        if lines and lines[-1][1] == s.speaker:
            lines[-1][2].append(s.text)
        else:
            lines.append([stamp, s.speaker, [s.text]])
    return "\n".join(f"[{stamp}] {speaker}: {' '.join(parts)}" for stamp, speaker, parts in lines)


def _format_markdown(segments: list[Segment], started: float) -> str:
    """The session transcript file: who spoke when, one line per stretch of speech.

    System audio is the interviewer, the microphone is the respondent. A new line
    starts when the speaker changes, after a pause, or once a line covers
    MD_LINE_MAX_S, so a long monologue still gets a timestamp every minute.
    """
    lines, current = [], None  # current: [speaker, first speech time, last end time, texts]
    for s in segments:
        if not s.text:
            continue
        at = s.speech_at or s.started
        if current and current[0] == s.speaker and at - current[2] <= MD_PAUSE_S and at - current[1] < MD_LINE_MAX_S:
            current[2] = max(current[2], s.ended or at)
            current[3].append(s.text)
            continue
        current = [s.speaker, at, s.ended or at, [s.text]]
        lines.append(current)
    title = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
    body = "\n\n".join(f"**{MD_ROLES[speaker]}:** [{time.strftime('%H:%M:%S', time.localtime(at))}] "
                        f"\"{' '.join(texts)}\"" for speaker, at, _, texts in lines)
    return f"# Transcript — {title}\n\n{body}"


def _stop_quietly(src):
    try:
        src.stop()
    except Exception:
        pass
