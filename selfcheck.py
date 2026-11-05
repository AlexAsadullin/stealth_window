"""`python main.py --check`: verify every OS-specific piece on this machine.

Prints one line per check and exits with 1 if anything essential failed. It
takes a few seconds: it records 2 s of microphone and system audio, takes one
screenshot (kept local) and asks GigaChat for a token. Nothing is sent to
GigaChat except the token request.
"""
import importlib
import os
import sys
import time

from platform_support import IS_LINUX, IS_MAC, cache_dir, data_dir, describe, is_wayland

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


class _Report:
    def __init__(self):
        self.failed = False

    def line(self, status: str, name: str, detail: str = ""):
        if status == FAIL:
            self.failed = True
        print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    def run(self, name: str, check, essential=True):
        try:
            status, detail = check()
        except Exception as exc:
            status, detail = (FAIL if essential else WARN), f"{type(exc).__name__}: {exc}"
        self.line(status, name, detail)


def run_checks() -> int:
    report = _Report()
    print(f"StealthChat self-check on {describe()}\n", flush=True)
    report.run("Python version", _python)
    report.run("Packages", _packages)
    report.run("Data folders", _folders)
    report.run("GigaChat key and certificate", _gigachat)

    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication(sys.argv)
    window = QWidget()
    window.resize(10, 10)
    report.run("Hidden from screen sharing", lambda: _capture_proof(window), essential=False)
    report.run("Global hotkeys", lambda: _hotkeys(window), essential=False)
    report.run("Microphone", _microphone)
    report.run("System audio (the other side of a call)", _system_audio)
    report.run("Screenshots", lambda: _screenshot(window), essential=False)
    report.run("MP3 recording", _mp3)
    window.close()
    app.processEvents()

    python = os.path.relpath(sys.executable) if not os.path.relpath(sys.executable).startswith("..") else sys.executable
    print("\n" + ("Something essential is missing: see FAIL above." if report.failed else
                  f"All essential parts work. Start the app with:  {python} main.py"), flush=True)
    return 1 if report.failed else 0


def _python():
    if sys.version_info < (3, 10):
        return FAIL, f"{sys.version.split()[0]}: needs Python 3.10 or newer"
    return OK, sys.version.split()[0]


def _packages():
    needed = ["PySide6", "numpy", "httpx", "h2", "sounddevice", "matplotlib", "lameenc"]
    if IS_MAC:
        needed += ["AppKit", "Quartz", "ScreenCaptureKit", "CoreMedia", "AVFoundation"]
    else:
        needed += ["soundcard"]
    if IS_LINUX:
        needed += ["Xlib"]
    missing = []
    for name in needed:
        try:
            importlib.import_module(name)
        except Exception as exc:  # ImportError, or OSError when a system library is missing
            missing.append(f"{name} ({type(exc).__name__}: {exc})")
    if missing:
        return FAIL, "missing: " + "; ".join(missing) + ". Run: python install.py"
    return OK, f"{len(needed)} packages import"


def _folders():
    for folder in (data_dir() / "sessions", cache_dir()):
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    return OK, f"sessions in {data_dir() / 'sessions'}"


def _gigachat():
    from ai_client import CA_BUNDLE, GigaChatClient

    if not CA_BUNDLE.exists():
        return FAIL, f"certificate missing: {CA_BUNDLE}"
    client = GigaChatClient()  # raises if GIGACHAT_AUTH_KEY is not set
    try:
        client.token()
    finally:
        client.close()
    return OK, f"token received (scope {client.scope})"


def _capture_proof(window):
    from stealth import make_capture_proof

    window.show()
    ok = make_capture_proof(window)
    window.hide()
    if ok:
        return OK, "the window is left out of screen sharing, recordings and screenshots"
    if IS_LINUX:
        return WARN, "not possible on Linux: the window WILL be visible when you share your screen"
    return WARN, "this Windows version can't exclude windows from capture (needs Windows 10 2004+)"


def _hotkeys(window):
    from hotkeys import GlobalHotkeys

    hotkeys = GlobalHotkeys(window)
    if not hotkeys.is_global:
        why = "Wayland forbids them" if is_wayland() else "no global hotkey support here"
        return WARN, f"{why}; hotkeys work only while the app's window is focused"
    ok = hotkeys.set("selfcheck", "Ctrl+Alt+Shift+F12", lambda: None)
    hotkeys.set("selfcheck", "")
    backend = type(hotkeys.backend).__name__.strip("_").replace("Backend", "")
    return (OK, f"{backend} backend registers system-wide hotkeys") if ok else \
        (WARN, f"{backend} backend could not register a test hotkey")


def _record(source_class, seconds=2.0):
    received = []
    source = source_class(received.append)
    source.start()
    time.sleep(seconds)
    source.stop()
    import numpy as np

    audio = np.concatenate(received) if received else np.zeros(0, np.float32)
    return len(audio) / 16000, float(np.abs(audio).max()) if len(audio) else 0.0


def _microphone():
    from audio_capture import MicCapture

    got, peak = _record(MicCapture)
    if got < 1.0:
        return FAIL, f"only {got:.1f}s of audio in 2s"
    hint = " (silent: is the microphone muted, or access denied?)" if peak < 0.0005 else ""
    return OK, f"{got:.1f}s received in 2s, peak level {peak:.3f}{hint}"


def _system_audio():
    from audio_capture import SystemAudioCapture

    got, peak = _record(SystemAudioCapture)
    if got < 1.0:
        return FAIL, f"only {got:.1f}s of audio in 2s"
    note = "silent right now, which is fine if nothing is playing" if peak < 0.0005 else f"peak level {peak:.3f}"
    return OK, f"{got:.1f}s received in 2s ({note})"


def _screenshot(window):
    from screenshot import capture_relevant_screen

    shot = capture_relevant_screen(fallback=window.screen())
    return OK, f"{len(shot) // 1024} KB captured (not sent anywhere)"


def _mp3():
    import lameenc
    import numpy as np

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(48)
    encoder.set_in_sample_rate(16000)
    encoder.set_channels(2)
    data = encoder.encode(np.zeros((16000, 2), "<i2").tobytes()) + encoder.flush()
    return (OK, "encoder works") if data else (FAIL, "encoder produced nothing")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.exit(run_checks())
