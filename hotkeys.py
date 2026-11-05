"""System-wide hotkeys that fire no matter which app or desktop is focused.

macOS:   Carbon RegisterEventHotKey. Needs no Accessibility permission and consumes
         the key press, so the combination no longer reaches other apps.
Windows: Win32 RegisterHotKey. Also consumes the key press.
Linux:   XGrabKey on X11 (python-xlib). Also consumes the key press.
Fallback (Wayland, or a backend that fails): application-wide Qt shortcuts,
which only work while this app's window has focus.

Key sequences are Qt's portable text ("Ctrl+Shift+Return"). Qt maps "Ctrl" to ⌘
on macOS and to the Ctrl key on Windows/Linux, so one setting means the natural
shortcut on each OS.
"""
import ctypes
import threading
import time

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut

from platform_support import IS_LINUX, IS_MAC, IS_WINDOWS, is_wayland

# ---------- Qt key -> macOS virtual key code (ANSI layout positions) ----------
_CHAR_KEYCODES = {
    "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9,
    "B": 11, "Q": 12, "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "O": 31, "U": 32, "[": 33, "I": 34, "P": 35, "L": 37, "J": 38,
    "'": 39, "K": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "N": 45, "M": 46, ".": 47,
    "`": 50,
}
# Qt sometimes records Shift+[ as "{" etc.; map back to the physical key + Shift.
_SHIFTED = {"{": "[", "}": "]", "|": "\\", "<": ",", ">": ".", "?": "/", ":": ";",
            '"': "'", "_": "-", "+": "=", "~": "`", "!": "1", "@": "2", "#": "3",
            "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}
_SPECIAL_KEYCODES = {
    Qt.Key_Return: (36, 76),  # main Return and keypad Enter
    Qt.Key_Enter: (76, 36),
    Qt.Key_Tab: (48,), Qt.Key_Space: (49,), Qt.Key_Backspace: (51,), Qt.Key_Escape: (53,),
    Qt.Key_Delete: (117,), Qt.Key_Home: (115,), Qt.Key_End: (119,),
    Qt.Key_PageUp: (116,), Qt.Key_PageDown: (121,),
    Qt.Key_Left: (123,), Qt.Key_Right: (124,), Qt.Key_Down: (125,), Qt.Key_Up: (126,),
    Qt.Key_F1: (122,), Qt.Key_F2: (120,), Qt.Key_F3: (99,), Qt.Key_F4: (118,),
    Qt.Key_F5: (96,), Qt.Key_F6: (97,), Qt.Key_F7: (98,), Qt.Key_F8: (100,),
    Qt.Key_F9: (101,), Qt.Key_F10: (109,), Qt.Key_F11: (103,), Qt.Key_F12: (111,),
}

# Carbon modifier masks
_CMD, _SHIFT, _OPTION, _CONTROL = 0x100, 0x200, 0x800, 0x1000


def _parse(seq: str):
    """'Ctrl+Shift+Return' -> (Qt key, Qt modifiers, printable base character or ""), or None."""
    ks = QKeySequence(seq)
    if ks.isEmpty():
        return None
    combo = ks[0]
    key, mods = combo.key(), combo.keyboardModifiers()
    ch = chr(key.value) if 0x20 < key.value < 0x7F else ""
    if ch in _SHIFTED:  # Qt sometimes records Shift+[ as "{": back to the physical key + Shift
        ch, mods = _SHIFTED[ch], mods | Qt.ShiftModifier
    return key, mods, ch.upper()


def _mac_combo(seq: str):
    """'Ctrl+Shift+Return' -> ([keycodes], carbon_modifiers), or None if unsupported.

    In Qt on macOS, Ctrl means ⌘ and Meta means the Control key.
    """
    parsed = _parse(seq)
    if parsed is None:
        return None
    key, mods, ch = parsed
    carbon = 0
    if mods & Qt.ControlModifier:
        carbon |= _CMD
    if mods & Qt.ShiftModifier:
        carbon |= _SHIFT
    if mods & Qt.AltModifier:
        carbon |= _OPTION
    if mods & Qt.MetaModifier:
        carbon |= _CONTROL

    if key in _SPECIAL_KEYCODES:
        return list(_SPECIAL_KEYCODES[key]), carbon
    if ch in _CHAR_KEYCODES:
        return [_CHAR_KEYCODES[ch]], carbon
    return None


# ---------- Windows: Qt key -> virtual-key code ----------
_WIN_SPECIAL = {
    Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D, Qt.Key_Tab: 0x09, Qt.Key_Space: 0x20, Qt.Key_Backspace: 0x08,
    Qt.Key_Escape: 0x1B, Qt.Key_Delete: 0x2E, Qt.Key_Home: 0x24, Qt.Key_End: 0x23, Qt.Key_PageUp: 0x21,
    Qt.Key_PageDown: 0x22, Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
    **{getattr(Qt, f"Key_F{i}"): 0x6F + i for i in range(1, 13)},  # F1 = 0x70
}
_WIN_PUNCT = {";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
              "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT, _MOD_WIN, _MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000


def _win_combo(seq: str):
    """'Ctrl+Shift+Return' -> (virtual-key code, MOD_* flags), or None if unsupported."""
    parsed = _parse(seq)
    if parsed is None:
        return None
    key, mods, ch = parsed
    flags = _MOD_NOREPEAT  # holding the keys down fires once, not on every auto-repeat
    if mods & Qt.ControlModifier:
        flags |= _MOD_CONTROL
    if mods & Qt.AltModifier:
        flags |= _MOD_ALT
    if mods & Qt.ShiftModifier:
        flags |= _MOD_SHIFT
    if mods & Qt.MetaModifier:  # on Windows Qt's Meta is the Windows key
        flags |= _MOD_WIN
    if key in _WIN_SPECIAL:
        return _WIN_SPECIAL[key], flags
    if ch.isalnum() and ch.isascii():
        return ord(ch), flags  # VK codes for A-Z and 0-9 are their ASCII codes
    if ch in _WIN_PUNCT:
        return _WIN_PUNCT[ch], flags
    return None


# ---------- Linux (X11): Qt key -> keysym name ----------
_X11_SPECIAL = {
    Qt.Key_Return: "Return", Qt.Key_Enter: "KP_Enter", Qt.Key_Tab: "Tab", Qt.Key_Space: "space",
    Qt.Key_Backspace: "BackSpace", Qt.Key_Escape: "Escape", Qt.Key_Delete: "Delete", Qt.Key_Home: "Home",
    Qt.Key_End: "End", Qt.Key_PageUp: "Prior", Qt.Key_PageDown: "Next", Qt.Key_Left: "Left", Qt.Key_Up: "Up",
    Qt.Key_Right: "Right", Qt.Key_Down: "Down", **{getattr(Qt, f"Key_F{i}"): f"F{i}" for i in range(1, 13)},
}
_X11_PUNCT = {"[": "bracketleft", "]": "bracketright", ",": "comma", ".": "period", "/": "slash",
              ";": "semicolon", "'": "apostrophe", "\\": "backslash", "-": "minus", "=": "equal", "`": "grave"}
_X_SHIFT, _X_LOCK, _X_CONTROL, _X_MOD1, _X_MOD2, _X_MOD4 = 1, 2, 4, 8, 16, 64  # Mod1 = Alt, Mod2 = NumLock, Mod4 = Super


def _x11_combo(seq: str):
    """'Ctrl+Shift+Return' -> ([keysym names], X modifier mask), or None if unsupported."""
    parsed = _parse(seq)
    if parsed is None:
        return None
    key, mods, ch = parsed
    mask = 0
    for flag, bit in ((Qt.ControlModifier, _X_CONTROL), (Qt.ShiftModifier, _X_SHIFT),
                      (Qt.AltModifier, _X_MOD1), (Qt.MetaModifier, _X_MOD4)):
        if mods & flag:
            mask |= bit
    if key in (Qt.Key_Return, Qt.Key_Enter):
        return ["Return", "KP_Enter"], mask  # main Return and keypad Enter, like on macOS
    if key in _X11_SPECIAL:
        return [_X11_SPECIAL[key]], mask
    if ch.isalnum() and ch.isascii():
        return [ch.lower()], mask
    if ch in _X11_PUNCT:
        return [_X11_PUNCT[ch]], mask
    return None


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class _MacBackend:
    SIGNATURE = _fourcc("stwn")

    def __init__(self, on_fire):
        c = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        c.GetApplicationEventTarget.restype = ctypes.c_void_p
        c.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_ulong,
                                          ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p)]
        c.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                        ctypes.c_void_p]
        c.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
                                          ctypes.c_void_p, ctypes.c_uint32,
                                          ctypes.POINTER(ctypes.c_void_p)]
        c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        self.c = c
        self.on_fire = on_fire

        def handler(_call_ref, event, _user_data):
            hk = _EventHotKeyID()
            c.GetEventParameter(event, _fourcc("----"), _fourcc("hkid"), None,
                                ctypes.sizeof(hk), None, ctypes.byref(hk))
            if hk.signature == self.SIGNATURE:
                self.on_fire(hk.id)
            return 0

        self._handler = _HANDLER(handler)  # keep a reference: Carbon holds a raw pointer
        spec = _EventTypeSpec(_fourcc("keyb"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
        ref = ctypes.c_void_p()
        status = c.InstallEventHandler(c.GetApplicationEventTarget(), self._handler, 1,
                                       ctypes.byref(spec), None, ctypes.byref(ref))
        if status != 0:
            raise OSError(f"InstallEventHandler failed: {status}")

    def register(self, hotkey_id: int, seq: str):
        """Returns a list of native refs; an empty list means the combo couldn't be registered."""
        combo = _mac_combo(seq)
        if combo is None:
            return []
        keycodes, mods = combo
        refs = []
        for code in keycodes:
            ref = ctypes.c_void_p()
            status = self.c.RegisterEventHotKey(code, mods, _EventHotKeyID(self.SIGNATURE, hotkey_id),
                                                self.c.GetApplicationEventTarget(), 0, ctypes.byref(ref))
            if status == 0:
                refs.append(ref)
        return refs

    def unregister(self, refs):
        for ref in refs:
            self.c.UnregisterEventHotKey(ref)


class _WindowsBackend(QAbstractNativeEventFilter):
    """RegisterHotKey on our window; WM_HOTKEY arrives through Qt's native event filter."""

    WM_HOTKEY = 0x0312

    def __init__(self, on_fire, hwnd: int):
        super().__init__()
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self._msg_type = wintypes.MSG
        self.hwnd, self.on_fire = hwnd, on_fire
        QCoreApplication.instance().installNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        # Window messages come as "windows_generic_MSG"; the dispatcher's own loop as "windows_dispatcher_MSG".
        if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = self._msg_type.from_address(int(message))
            if msg.message == self.WM_HOTKEY:
                self.on_fire(int(msg.wParam))
                return True, 0
        return False, 0

    def register(self, hotkey_id: int, seq: str):
        combo = _win_combo(seq)
        if combo is None:
            return []
        vk, flags = combo
        if not self.user32.RegisterHotKey(self.hwnd, hotkey_id, flags, vk):
            return []  # usually ERROR_HOTKEY_ALREADY_REGISTERED: another app owns this combination
        return [hotkey_id]

    def unregister(self, refs):
        for hotkey_id in refs:
            self.user32.UnregisterHotKey(self.hwnd, hotkey_id)


class _LinuxBackend(QObject):
    """XGrabKey on the X11 root window (python-xlib, pure Python): like on macOS and Windows the
    combination is consumed. Wayland has no equivalent, so there the in-app fallback is used.

    Xlib connections aren't thread-safe, so one thread owns the connection: it grabs and
    releases keys on request and turns key presses into a Qt signal.
    """

    fired = Signal(int)
    # NumLock and CapsLock change the modifier state; grab every variant so they don't matter.
    _LOCK_VARIANTS = (0, _X_LOCK, _X_MOD2, _X_LOCK | _X_MOD2)

    def __init__(self, on_fire):
        super().__init__()
        if is_wayland():
            raise OSError("Wayland does not let apps register global hotkeys")
        import queue

        from Xlib.display import Display

        self._display = Display()  # raises without an X server ($DISPLAY)
        self._root = self._display.screen().root
        self._requests = queue.Queue()  # (function, args, result queue)
        self._grabs: dict[tuple[int, int], int] = {}  # (keycode, mask) -> hotkey id
        self.fired.connect(on_fire)  # queued: emitted on the X thread, handled on Qt's
        threading.Thread(target=self._run, name="x11-hotkeys", daemon=True).start()

    def register(self, hotkey_id: int, seq: str):
        combo = _x11_combo(seq)
        return self._call(self._grab, hotkey_id, combo) if combo else []

    def unregister(self, refs):
        self._call(self._ungrab, refs)

    def _call(self, fn, *args):
        """Run fn on the X thread and wait for its result."""
        import queue

        result = queue.Queue()
        self._requests.put((fn, args, result))
        try:
            return result.get(timeout=2)
        except queue.Empty:
            return []

    def _run(self):
        from Xlib import X

        while True:
            while not self._requests.empty():
                fn, args, result = self._requests.get()
                result.put(fn(*args))
            while self._display.pending_events():
                event = self._display.next_event()
                if event.type == X.KeyPress:
                    mask = event.state & (_X_SHIFT | _X_CONTROL | _X_MOD1 | _X_MOD4)
                    hotkey_id = self._grabs.get((event.detail, mask))
                    if hotkey_id is not None:
                        self.fired.emit(hotkey_id)
            time.sleep(0.02)

    def _grab(self, hotkey_id: int, combo):
        from Xlib import X, XK, error

        names, mask = combo
        grabbed = []
        for name in names:
            keycode = self._display.keysym_to_keycode(XK.string_to_keysym(name))
            if not keycode or (keycode, mask) in self._grabs:
                continue
            catch = error.CatchError(error.BadAccess)  # BadAccess: another app grabbed it first
            for lock in self._LOCK_VARIANTS:
                self._root.grab_key(keycode, mask | lock, True, X.GrabModeAsync, X.GrabModeAsync, onerror=catch)
            self._display.sync()
            if catch.get_error():
                for lock in self._LOCK_VARIANTS:
                    self._root.ungrab_key(keycode, mask | lock)
                continue
            self._grabs[(keycode, mask)] = hotkey_id
            grabbed.append((keycode, mask))
        self._display.flush()
        return grabbed

    def _ungrab(self, refs):
        for keycode, mask in refs:
            for lock in self._LOCK_VARIANTS:
                self._root.ungrab_key(keycode, mask | lock)
            self._grabs.pop((keycode, mask), None)
        self._display.flush()


class GlobalHotkeys(QObject):
    """Maps action names to key sequences and handlers.

    ``set()`` returns False when the combination could not be registered,
    usually because another app already owns it.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._actions = {}  # action -> {"seq", "handler", "id", "native"}
        self._enabled = True
        self._only = None  # while disabled: set of actions that stay active
        self.backend = None
        try:
            if IS_MAC:
                self.backend = _MacBackend(self._fire)
            elif IS_WINDOWS:
                self.backend = _WindowsBackend(self._fire, int(parent.winId()))
            elif IS_LINUX:
                self.backend = _LinuxBackend(self._fire)
        except Exception as exc:  # e.g. Wayland, or no X display
            print(f"warning: global hotkeys unavailable ({exc}); they work only while the window is focused")

    @property
    def is_global(self) -> bool:
        return self.backend is not None

    def set(self, action: str, seq: str, handler=None) -> bool:
        entry = self._actions.get(action)
        if entry is None:
            entry = {"id": len(self._actions) + 1, "native": None, "handler": handler}
            self._actions[action] = entry
        if handler is not None:
            entry["handler"] = handler
        self._release(entry)
        entry["seq"] = seq
        return self._grab(action, entry)

    def set_enabled(self, enabled: bool, keep=()):
        """Disable all hotkeys except those in ``keep`` (e.g. while recording new ones)."""
        self._enabled, self._only = enabled, None if enabled else set(keep)
        for action, entry in self._actions.items():
            self._release(entry)
            self._grab(action, entry)

    def ok(self, action: str) -> bool:
        entry = self._actions.get(action)
        return bool(entry and (not entry["seq"] or entry["native"]))

    def _active(self, action):
        return self._enabled or action in self._only

    def _grab(self, action, entry) -> bool:
        if not entry["seq"] or not self._active(action):
            return True
        if self.backend:
            entry["native"] = self.backend.register(entry["id"], entry["seq"]) or None
        else:
            sc = QShortcut(QKeySequence(entry["seq"]), self.parent())
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(entry["handler"])
            entry["native"] = sc
        return entry["native"] is not None

    def _release(self, entry):
        native, entry["native"] = entry["native"], None
        if native is None:
            return
        if self.backend:
            self.backend.unregister(native)
        else:
            native.setEnabled(False)
            native.deleteLater()

    def _fire(self, hotkey_id: int):
        for entry in self._actions.values():
            if entry["id"] == hotkey_id and entry["handler"]:
                entry["handler"]()
                return
