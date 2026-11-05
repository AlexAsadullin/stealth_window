"""Exclude a Qt top-level window from screen capture / screen sharing / screenshots.

macOS:   NSWindowSharingNone — the window is left out of every capture.
Windows: SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE), Windows 10 version 2004+.
Linux:   impossible — X11 and Wayland have no such API, so the window is visible
         in screen sharing and screenshots; the app says so in its title bar.
"""
from platform_support import IS_MAC, IS_WINDOWS


def make_capture_proof(widget) -> bool:
    """Call after widget.show(). Returns True if the window is now hidden from capture."""
    if IS_MAC:
        return _macos(widget)
    if IS_WINDOWS:
        return _windows(widget)
    return False


def hide_from_dock():
    """macOS: no Dock icon or app switcher entry (they would show up in a shared screen).

    Windows/Linux get the same effect from the window being a tool window (see ChatWindow).
    """
    if not IS_MAC:
        return
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def activate_app():
    """Bring this app to the front (needed to type into it after a global hotkey).

    On Windows the process that received the hotkey may take the foreground, and
    Qt's activateWindow() does it; only macOS needs the app activated first.
    """
    if not IS_MAC:
        return
    from AppKit import NSApplication

    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def hide_app():
    """Hand focus back to the previously active app (after hiding our only window)."""
    if IS_MAC:
        from AppKit import NSApplication

        NSApplication.sharedApplication().hide_(None)


def unhide_app():
    """Undo hide_app() without taking focus from the current app."""
    if IS_MAC:
        from AppKit import NSApplication

        NSApplication.sharedApplication().unhideWithoutActivation()


def _macos(widget) -> bool:
    import objc
    from AppKit import (
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowSharingNone,
    )

    view = objc.objc_object(c_void_p=int(widget.winId()))
    window = view.window()
    if window is None:
        return False
    window.setSharingType_(NSWindowSharingNone)
    # Stay visible over full-screen apps and on every Space.
    window.setCollectionBehavior_(
        window.collectionBehavior()
        | NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    return window.sharingType() == NSWindowSharingNone


def _windows(widget) -> bool:
    import ctypes

    from ctypes import wintypes

    WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+: the window is simply absent from captures
    WDA_MONITOR = 0x01  # older Windows: the window shows as a black box instead
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    hwnd = int(widget.winId())
    if user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        return True
    user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR)  # a black box is still better than readable text
    return False
