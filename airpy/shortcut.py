"""Create/remove the desktop shortcut (AirPy.lnk)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import APP_NAME
from .autostart import project_root

_LOG = logging.getLogger(__name__)
_FILE = f"{APP_NAME}.lnk"


def _shell():
    import comtypes
    import comtypes.client

    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    return comtypes.client.CreateObject("WScript.Shell", dynamic=True)


def desktop_dir() -> Path:
    """The user's desktop folder (the right path even if it was moved to OneDrive)."""
    return Path(str(_shell().SpecialFolders("Desktop")))


def create(directory: Path | None = None) -> Path:
    """Create the shortcut (updates it if it exists) and return its path."""
    shell = _shell()
    target_dir = Path(directory) if directory else Path(str(shell.SpecialFolders("Desktop")))
    path = target_dir / _FILE
    root = project_root()
    link = shell.CreateShortcut(str(path))
    link.TargetPath = str(Path(sys.executable).with_name("pythonw.exe"))
    link.Arguments = f'"{root / "AirPy.pyw"}"'
    link.WorkingDirectory = str(root)
    link.IconLocation = str(root / "assets" / "airpy.ico")
    link.Description = "AirPy - stream PC audio to AirPlay speakers"
    link.Save()
    return path


def remove(directory: Path | None = None) -> bool:
    """Delete the shortcut if it exists. True if it was deleted."""
    try:
        path = (Path(directory) if directory else desktop_dir()) / _FILE
        if path.exists():
            path.unlink()
            return True
    except Exception as exc:
        _LOG.warning("Could not delete the shortcut: %s", exc)
    return False
