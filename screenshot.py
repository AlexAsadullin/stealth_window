"""Full-screen screenshot as PNG bytes, on macOS, Windows and Linux (X11).

On macOS and Windows the app's own window is excluded from capture, so it never
appears in the shot (on Linux the window steps aside while the shot is taken).
macOS needs the Screen Recording permission for the terminal or IDE that
launched the app; without it the capture "succeeds" but shows only the
wallpaper, so the permission is checked up front. Wayland does not let apps
take screenshots at all.
"""
import os
import subprocess
import tempfile

from PySide6.QtCore import QBuffer, QIODevice, QPoint
from PySide6.QtGui import QCursor, QGuiApplication

from platform_support import IS_MAC, IS_WINDOWS, is_wayland


class ScreenshotError(OSError):
    pass


# Apps whose window marks the screen the conversation is on. Messengers (Telegram, Slack, ...)
# are left out: they are often open just for chatting, on another screen.
CALL_APPS = ("zoom", "teams", "webex", "facetime", "skype", "telemost", "телемост", "jazz", "ktalk", "толк")


def capture_relevant_screen(fallback) -> bytes:
    """PNG of the one display the conversation is most likely on. Raises ScreenshotError.

    Only one screen is sent: with several screenshots in a request GigaChat
    mixes them up and may ignore the one that matters.
    """
    if is_wayland():
        raise ScreenshotError("Wayland does not let apps take screenshots; log in to an X11 session to use them")
    if IS_MAC:
        import Quartz

        if not Quartz.CGPreflightScreenCaptureAccess():
            raise ScreenshotError("no Screen Recording permission, so macOS returns only the wallpaper. Allow it "
                                  "for the app that launched this one (System Settings → Privacy & Security → "
                                  "Screen & System Audio Recording), then restart it.")
    shot = capture_screen(_pick_screen(fallback))
    if not shot:
        raise ScreenshotError("screen capture failed")
    return shot


def _pick_screen(fallback):
    """The display showing the biggest call-app window, else the one under the mouse, else ``fallback``."""
    screens = QGuiApplication.screens()
    if len(screens) == 1:
        return screens[0]
    if IS_WINDOWS:
        if screen := _windows_call_screen(screens):
            return screen
    if IS_MAC:
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
        calls = []
        for w in windows:
            owner = (w.get("kCGWindowOwnerName") or "").lower()
            b = w.get("kCGWindowBounds") or {}
            if w.get("kCGWindowLayer") == 0 and any(app in owner for app in CALL_APPS):
                calls.append((b.get("Width", 0) * b.get("Height", 0), b))
        if calls:
            b = max(calls, key=lambda c: c[0])[1]
            centre = QPoint(int(b["X"] + b["Width"] / 2), int(b["Y"] + b["Height"] / 2))  # global points, like Qt
            if screen := QGuiApplication.screenAt(centre):
                return screen
    return QGuiApplication.screenAt(QCursor.pos()) or fallback


def _windows_call_screen(screens):
    """The screen holding the biggest visible window whose title names a call app (e.g. "Zoom Meeting").

    Matched by monitor device name (\\\\.\\DISPLAY1), which Qt also uses as QScreen.name(),
    so mixed-DPI setups need no coordinate conversion.
    """
    import ctypes
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32 = ctypes.WinDLL("user32")
    # Declared types: undeclared, ctypes would squeeze 64-bit handles into a C int.
    for name, restype, argtypes in (
            ("EnumWindows", wintypes.BOOL, [enum_proc, wintypes.LPARAM]),
            ("IsWindowVisible", wintypes.BOOL, [wintypes.HWND]),
            ("IsIconic", wintypes.BOOL, [wintypes.HWND]),
            ("GetWindowTextLengthW", ctypes.c_int, [wintypes.HWND]),
            ("GetWindowTextW", ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
            ("GetWindowRect", wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
            ("MonitorFromWindow", wintypes.HMONITOR, [wintypes.HWND, wintypes.DWORD]),
            ("GetMonitorInfoW", wintypes.BOOL, [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)])):
        getattr(user32, name).restype, getattr(user32, name).argtypes = restype, argtypes

    found = []  # (area, hwnd)

    @enum_proc
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                title = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, title, n + 1)
                if any(app in title.value.lower() for app in CALL_APPS):
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    found.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return None
    monitor = user32.MonitorFromWindow(max(found)[1], 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    return next((s for s in screens if s.name() == info.szDevice), None)


def capture_screen(screen) -> bytes:
    """Capture the given QScreen. Returns b"" on failure."""
    if IS_MAC:
        g = screen.geometry()  # global coordinates in points, as screencapture -R expects
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            subprocess.run(["screencapture", "-x", "-t", "png",
                            f"-R{g.x()},{g.y()},{g.width()},{g.height()}", path],
                           capture_output=True, timeout=10)
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.remove(path)

    pixmap = screen.grabWindow(0)
    if pixmap.isNull():
        return b""
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pixmap.save(buf, "PNG")
    return bytes(buf.data())
