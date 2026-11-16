"""AirPy launcher: starts the app without opening a console window.

Uses the project's virtual environment (.venv) if there is one, otherwise the Python that runs this
script. With ``--hidden`` no window is shown, only the tray icon.
"""

import ctypes
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def find_python() -> Path:
    if VENV_PYTHON.exists():
        return VENV_PYTHON
    current = Path(sys.executable)
    console = current.with_name("python.exe")  # running under pythonw.exe: use the python.exe next to it
    return console if console.exists() else current


def main() -> None:
    python = find_python()
    probe = subprocess.run(
        [str(python), "-c", "import airpy.app"],
        cwd=str(ROOT),
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if probe.returncode != 0:
        ctypes.windll.user32.MessageBoxW(
            None,
            "AirPy's dependencies were not found.\n\nRun install.bat in the project folder first.",
            "AirPy",
            0x10,
        )
        return
    subprocess.Popen(
        [str(python), "-m", "airpy", *sys.argv[1:]],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )


if __name__ == "__main__":
    main()
