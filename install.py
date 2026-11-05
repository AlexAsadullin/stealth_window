"""One-step setup on macOS, Windows or Linux:  python install.py

1. checks the Python version,
2. creates .venv next to this file (or reuses it),
3. installs requirements.txt into it (the right packages for this OS),
4. on Linux, checks the system libraries Python packages can't bring along,
5. creates .env from .env.example if there is none,
6. runs the self-check (main.py --check).

Uses only the standard library, so it runs before anything is installed.
"""
import ctypes.util
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
IS_WINDOWS = platform.system() == "Windows" or os.name == "nt"
IS_LINUX = platform.system() == "Linux"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def run(*cmd):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call([str(c) for c in cmd])


def main():
    sys.stdout.reconfigure(line_buffering=True)  # our lines before the child processes' output, not after
    print(f"Setting up StealthChat on {platform.system()} {platform.release()} ({platform.machine()})\n")
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10 or newer is needed, this is {platform.python_version()}. "
                 "Get it from https://www.python.org/downloads/")

    print("1. Virtual environment")
    if not venv_python().exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
    else:
        print(f"   reusing {VENV}")

    print("2. Packages for this OS")
    uv = shutil.which("uv")
    has_pip = subprocess.call([str(venv_python()), "-m", "pip", "--version"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    if uv and not has_pip:  # a venv made by uv has no pip; uv installs into it directly
        run(uv, "pip", "install", "--python", venv_python(), "-r", HERE / "requirements.txt")
    else:
        if not has_pip:
            run(venv_python(), "-m", "ensurepip", "--upgrade")
        run(venv_python(), "-m", "pip", "install", "--upgrade", "pip")
        run(venv_python(), "-m", "pip", "install", "-r", HERE / "requirements.txt")

    print("3. System libraries")
    if not IS_LINUX:
        print("   nothing extra needed on this OS")
    else:
        missing = [f"{lib}: {why}" for lib, why in (
            ("portaudio", "the microphone — install libportaudio2 (Debian/Ubuntu) or portaudio (Fedora/Arch)"),
            ("pulse", "system audio — install libpulse0, or PipeWire with pipewire-pulse"),
            ("xcb-cursor", "Qt's window system plugin — install libxcb-cursor0"),
        ) if not ctypes.util.find_library(lib)]
        for line in missing:
            print(f"   missing {line}")
        if not missing:
            print("   all present")
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            print("   note: this is a Wayland session — global hotkeys and screenshots need an X11 session")

    print("4. Settings")
    env, example = HERE / ".env", HERE / ".env.example"
    if env.exists():
        print("   .env exists")
    else:
        shutil.copyfile(example, env)
        print(f"   created {env} — open it and paste your GigaChat authorization key after GIGACHAT_AUTH_KEY=")

    print("5. Self-check\n")
    code = subprocess.call([str(venv_python()), str(HERE / "main.py"), "--check"], cwd=HERE)
    start = f"{VENV.name}\\Scripts\\python main.py" if IS_WINDOWS else f"{VENV.name}/bin/python main.py"
    print(f"\nStart the app from this folder with:  {start}")
    sys.exit(code)


if __name__ == "__main__":
    main()
