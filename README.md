# StealthChat

An always-on-top assistant window that is hidden from screen sharing, listens to
the dialog (your microphone + everything the computer plays) and answers with
GigaChat on a hotkey. Runs on macOS, Windows and Linux from the same code.

## Setup (any OS)

Needs Python 3.10+ (on macOS not the built-in 3.9: use Homebrew's or python.org's).

```
python install.py
```

It creates `.venv`, installs the packages this OS needs, creates `.env` from
`.env.example` (paste your GigaChat authorization key there) and runs a self-check
of every part: GigaChat, microphone, system audio, hotkeys, hiding from capture,
screenshots, MP3 recording. Re-run the check any time:

| OS | Self-check | Start the app |
|---|---|---|
| macOS / Linux | `.venv/bin/python main.py --check` | `.venv/bin/python main.py` |
| Windows | `.venv\Scripts\python main.py --check` | `.venv\Scripts\pythonw main.py` |

On Windows, `pythonw` starts the app without a console window, which would
otherwise show up when you share your screen. Its log is still written to
`app.log` in the session folder.

## Testing status

- [x] **macOS** — tested end to end (`--check` all OK, real calls, sound tests)
- [ ] **Windows** — not run yet: code paths reviewed and exercised on macOS, all packages have Windows wheels
- [ ] **Linux** — not run yet: code paths reviewed, all packages have x86-64 Linux wheels

Run `main.py --check` on a new machine first; `app.log` in the session folder shows what went wrong.

## What each OS gives you

| | macOS | Windows (untested) | Linux (untested) |
|---|---|---|---|
| Hidden from screen sharing | yes | yes (Windows 10 2004+) | **no** — not possible; the title bar warns |
| System audio (the other side of a call) | ScreenCaptureKit | WASAPI loopback | PulseAudio / PipeWire monitor |
| Global hotkeys | yes | yes | X11 only (not Wayland) |
| Screenshots | yes | yes | X11 only (not Wayland) |
| No Dock / taskbar entry | yes | yes | depends on the desktop |

Hotkeys use the same setting on every OS: "Ctrl" in Settings is ⌘ on macOS and
the Ctrl key on Windows/Linux.

### macOS permissions
Allow **Microphone** and **Screen & System Audio Recording** for the app you start
it from (Terminal, PyCharm, …) in System Settings → Privacy & Security, then restart it.

### Linux system libraries
`install.py` reports missing ones. On Debian/Ubuntu:
`sudo apt install libportaudio2 libpulse0 libxcb-cursor0`
(PipeWire desktops need `pipewire-pulse`, which they normally have.)

## Where things are saved

One folder per session with `transcript.md`, `answers.md`, `audio.mp3` and `app.log`:

- macOS: `~/Library/Application Support/StealthChat/sessions/`
- Windows: `%LOCALAPPDATA%\StealthChat\sessions\`
- Linux: `~/.local/share/StealthChat/sessions/`
