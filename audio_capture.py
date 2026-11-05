"""Audio sources: system output and microphone, on macOS, Windows and Linux.

Both deliver 16 kHz mono float32 numpy chunks to a callback, from a background
thread, continuously (silence arrives as zeros, so a gap means a dead stream).

System audio — everything the computer plays (calls, videos, music):
    macOS    ScreenCaptureKit. Needs the Screen & System Audio Recording permission
             for the app that launched this one (Terminal, PyCharm, ...).
    Windows  WASAPI loopback of the default output device (via soundcard). No permission.
    Linux    the PulseAudio / PipeWire "Monitor of …" source of the default output.
Microphone: PortAudio (sounddevice) everywhere; macOS asks for the Microphone permission.
"""
import threading
import warnings

import numpy as np

from platform_support import IS_MAC, IS_WINDOWS

SAMPLE_RATE = 16000
BLOCK = SAMPLE_RATE // 10  # 100 ms chunks


def SystemAudioCapture(on_audio):
    """The system-audio source for this OS (same interface on all: start(), stop())."""
    return _MacSystemAudio(on_audio) if IS_MAC else _LoopbackSystemAudio(on_audio)


def _to_mono_16k(data: np.ndarray, rate: int) -> np.ndarray:
    """Mix down to mono and resample to SAMPLE_RATE (only needed when a device refuses 16 kHz)."""
    if data.ndim == 2:
        data = data.mean(axis=1)
    data = data.astype(np.float32, copy=False)
    if rate == SAMPLE_RATE or not len(data):
        return data
    if rate % SAMPLE_RATE == 0:  # 48 kHz, 32 kHz: average groups of samples (also filters aliasing)
        k = rate // SAMPLE_RATE
        n = len(data) // k * k
        return data[:n].reshape(-1, k).mean(axis=1)
    positions = np.arange(0, len(data), rate / SAMPLE_RATE)
    return np.interp(positions, np.arange(len(data)), data).astype(np.float32)


class MicCapture:
    def __init__(self, on_audio):
        self.on_audio = on_audio
        self._stream = None

    def start(self):
        import sounddevice as sd

        if IS_MAC:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

            # 1 = restricted, 2 = denied. Without access PortAudio silently records zeros.
            if AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio) in (1, 2):
                raise OSError("microphone access denied. Allow it for the app that launched this one "
                              "(System Settings → Privacy & Security → Microphone), then restart it.")

        try:  # most devices resample to 16 kHz for us (CoreAudio, MME, PulseAudio)
            rate = SAMPLE_RATE
            self._stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=BLOCK,
                                          callback=lambda indata, *_: self.on_audio(indata[:, 0].copy()))
        except sd.PortAudioError:  # this device only takes its native rate: resample ourselves
            rate = int(sd.query_devices(kind="input")["default_samplerate"])
            self._stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=rate // 10,
                                          callback=lambda indata, *_: self.on_audio(_to_mono_16k(indata, rate)))
        self._stream.start()

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class _LoopbackSystemAudio:
    """Windows: WASAPI loopback of the default output device. Linux: its PulseAudio/PipeWire monitor.

    Both run through the soundcard library on a thread of our own. When nothing
    plays, WASAPI delivers no data; soundcard then returns zeros, so the stream
    stays continuous like on the other platforms.
    """

    def __init__(self, on_audio):
        self.on_audio = on_audio
        self._stop = threading.Event()
        self._thread = None

    def start(self, timeout: float = 5.0):
        ready, failure = threading.Event(), []
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(ready, failure), name="loopback", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            self._stop.set()
            raise OSError("system audio loopback did not start")
        if failure:
            raise OSError(f"system audio loopback: {failure[0]}")

    def _run(self, ready: threading.Event, failure: list):
        try:
            if IS_WINDOWS:  # this thread talks to WASAPI, which is COM: initialise COM for it (multithreaded)
                import ctypes
                ctypes.windll.ole32.CoInitializeEx(None, 0)
            import soundcard as sc

            warnings.filterwarnings("ignore", message="data discontinuity")  # harmless WASAPI glitch report
            speaker = sc.default_speaker()
            if speaker is None:
                raise OSError("no default output device")
            source = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            # Windows: a 100 ms WASAPI buffer, so a busy moment in the app doesn't drop audio
            # (its default is ~10 ms). PulseAudio/PipeWire pick a sensible size themselves.
            options = {"blocksize": BLOCK} if IS_WINDOWS else {}
            with source.recorder(samplerate=SAMPLE_RATE, **options) as recorder:
                ready.set()
                while not self._stop.is_set():
                    data = recorder.record(numframes=BLOCK)  # blocks ~100 ms; zeros when nothing plays
                    self.on_audio(_to_mono_16k(data, SAMPLE_RATE))
        except Exception as exc:
            failure.append(f"{type(exc).__name__}: {exc}")
            ready.set()

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


class _MacSystemAudio:
    """Everything the Mac plays (calls, videos, music), except this app's own sound."""

    def __init__(self, on_audio):
        self.on_audio = on_audio
        self._stream = None
        self._output = None

    def start(self, attempts: int = 3):
        """ScreenCaptureKit's daemon (replayd) can get into a state where it drops some
        requests and answers others (seen after a process was killed mid-capture), so a
        request that gets no answer is retried at once instead of failing the source."""
        for attempt in range(attempts):
            try:
                self._start_once()
                return
            except OSError:
                self.stop(timeout=2.0)  # don't leave a half-started stream behind
                if attempt == attempts - 1:
                    raise

    def _start_once(self, timeout: float = 4.0):
        import ScreenCaptureKit as SCK

        content, error = _wait(SCK.SCShareableContent.getShareableContentWithCompletionHandler_, timeout)
        if error is not None or content is None or not content.displays():
            raise OSError(f"no access to screen content: {error}. Allow Screen & System Audio Recording "
                          f"for the app that launched this one, then restart it.")
        display = content.displays()[0]
        content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setExcludesCurrentProcessAudio_(True)
        config.setSampleRate_(SAMPLE_RATE)
        config.setChannelCount_(1)
        # Audio-only: keep the (mandatory) video stream as cheap as possible.
        config.setWidth_(2)
        config.setHeight_(2)
        config.setMinimumFrameInterval_((1, 1, 1, 0))  # CMTime: 1 frame per second
        config.setQueueDepth_(1)

        self._output = _make_output(self.on_audio)
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(content_filter, config, None)
        ok, error = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, SCK.SCStreamOutputTypeAudio, None, None)
        if not ok:
            raise OSError(f"could not add audio output: {error}")
        (error,) = _wait(self._stream.startCaptureWithCompletionHandler_, timeout)
        if error is not None:
            raise OSError(f"could not start system audio capture: {error}")

    def stop(self, timeout: float = 5.0):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                _wait(stream.stopCaptureWithCompletionHandler_, timeout)
            except OSError:
                pass  # replayd didn't answer; the stream dies with the process anyway


def _wait(method, timeout):
    """Call an Objective-C method that takes a completion handler and wait for its result."""
    done = threading.Event()
    result = []

    def handler(*args):
        result.extend(args)
        done.set()

    method(handler)
    if not done.wait(timeout):
        raise OSError("ScreenCaptureKit did not respond")
    return tuple(result)


_OUTPUT_CLASS = None


def _make_output(on_audio):
    global _OUTPUT_CLASS
    import CoreMedia
    import objc
    import ScreenCaptureKit as SCK
    from Foundation import NSObject

    if _OUTPUT_CLASS is None:
        class _AudioOutput(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
            def stream_didOutputSampleBuffer_ofType_(self, _stream, sample, kind):
                if kind != SCK.SCStreamOutputTypeAudio or not CoreMedia.CMSampleBufferIsValid(sample):
                    return
                block = CoreMedia.CMSampleBufferGetDataBuffer(sample)
                if block is None:
                    return
                length = CoreMedia.CMBlockBufferGetDataLength(block)
                status, data = CoreMedia.CMBlockBufferCopyDataBytes(block, 0, length, None)
                if status == 0 and data:
                    # Mono float32; with more channels the buffer is planar, so channel 0 comes first.
                    frames = CoreMedia.CMSampleBufferGetNumSamples(sample)
                    self.callback(np.frombuffer(bytes(data), dtype=np.float32)[:frames].copy())

        _OUTPUT_CLASS = _AudioOutput

    output = _OUTPUT_CLASS.alloc().init()
    output.callback = on_audio
    return output
