"""Which operating system the app runs on, and where it keeps its files there.

Every OS-specific branch in the app checks these flags, so the same code runs on
macOS, Windows and Linux:

                        macOS                      Windows                    Linux
    system audio        ScreenCaptureKit           WASAPI loopback            PulseAudio/PipeWire monitor
    global hotkeys      Carbon RegisterEventHotKey Win32 RegisterHotKey       X11 via pynput (not Wayland)
    hidden from capture NSWindowSharingNone        WDA_EXCLUDEFROMCAPTURE     not possible (no such API)
    no taskbar/Dock     accessory app              tool window                tool window
    data folder         ~/Library/Application      %LOCALAPPDATA%\\StealthChat ~/.local/share/StealthChat
                        Support/StealthChat
"""
import os
import platform
from pathlib import Path

SYSTEM = platform.system()  # "Darwin", "Windows" or "Linux"
IS_MAC = SYSTEM == "Darwin"
IS_WINDOWS = SYSTEM == "Windows" or os.name == "nt"
IS_LINUX = SYSTEM == "Linux"

APP_NAME = "StealthChat"


def is_wayland() -> bool:
    """Wayland sessions forbid global hotkeys and screen grabs by ordinary apps."""
    return IS_LINUX and (os.environ.get("XDG_SESSION_TYPE") == "wayland" or "WAYLAND_DISPLAY" in os.environ)


def data_dir() -> Path:
    """Session recordings and transcripts."""
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


def cache_dir() -> Path:
    """Disposable files: rendered formulas, the last screenshot sent."""
    if IS_MAC:
        return Path.home() / "Library" / "Caches" / APP_NAME
    if IS_WINDOWS:
        return data_dir() / "Cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP_NAME


def describe() -> str:
    release = platform.mac_ver()[0] if IS_MAC else platform.release()
    extra = " (Wayland)" if is_wayland() else ""
    return f"{SYSTEM} {release}{extra}, {platform.machine()}, Python {platform.python_version()}"
