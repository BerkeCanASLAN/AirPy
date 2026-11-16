"""Start automatically when Windows signs in (HKCU\\...\\Run, current user only)."""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

from . import APP_NAME

_LOG = logging.getLogger(__name__)
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def command() -> str:
    """Command to run at sign-in: starts in the tray without opening a window."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = project_root() / "AirPy.pyw"
    return f'"{pythonw}" "{launcher}" --hidden'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def sync(enabled: bool) -> bool:
    """Bring the registry to the wanted state. True on success."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        ) as key:
            if enabled:
                wanted = command()
                try:
                    current, _ = winreg.QueryValueEx(key, APP_NAME)
                except OSError:
                    current = None
                if current != wanted:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, wanted)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        _LOG.error("Could not set up auto-start: %s", exc)
        return False
