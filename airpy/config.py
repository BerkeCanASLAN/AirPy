"""Persistent settings (%APPDATA%\\AirPy\\config.json)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from . import APP_NAME

_LOG = logging.getLogger(__name__)


def app_dir() -> Path:
    """Settings and log folder. Can be overridden with AIRPY_HOME (for tests)."""
    override = os.environ.get("AIRPY_HOME")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_receive_dir() -> Path:
    return Path.home() / "Downloads" / APP_NAME


DEFAULTS: dict[str, Any] = {
    # Keys of the targets to stream to ("pair:<tsid>" or a device id)
    "selected": [],
    # Connect automatically to the selected speakers when the app starts
    "auto_connect": True,
    # Start the app when Windows signs in
    "autostart": True,
    # Mute the PC's own speakers while streaming
    "mute_local": False,
    # Stereo pair mode: "leader" (leader only) or "both" (a separate stream to each speaker)
    "pair_mode": "leader",
    # Speaker buffer (ms). Low value = low latency, risk of dropouts on a weak network
    "latency_ms": 1500,
    # File sharing server
    "share_enabled": True,
    # Address shown to the phone: "tailscale" (if present, works from any network) or "lan" (local network)
    "share_via": "tailscale",
    "share_port": 53318,
    "share_token": "",
    "receive_dir": "",
    # Credentials of devices paired with a PIN (device id -> string)
    "credentials": {},
}


_OLD_LATENCY = {"standard": 1500, "low": 750, "ultra": 350}


class Config:
    """Simple thread-safe settings store that writes to disk on every change."""

    def __init__(self) -> None:
        self._path = app_dir() / "config.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._load()
        if not self._data.get("share_token"):
            self._data["share_token"] = secrets.token_urlsafe(9)
            self._save()
        if not self._data.get("receive_dir"):
            self._data["receive_dir"] = str(default_receive_dir())
            self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # corrupt file: carry on with the defaults
            _LOG.warning("Could not read the settings file (%s), using the defaults", exc)
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in DEFAULTS and isinstance(value, type(DEFAULTS[key])):
                    self._data[key] = value
            # Old version: named latency profile -> milliseconds
            if "latency_ms" not in raw and raw.get("latency") in _OLD_LATENCY:
                self._data["latency_ms"] = _OLD_LATENCY[raw["latency"]]

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._path)

    def get(self, key: str) -> Any:
        with self._lock:
            value = self._data[key]
            if isinstance(value, list):
                return list(value)
            if isinstance(value, dict):
                return dict(value)
            return value

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        with self._lock:
            if self._data.get(key) == value:
                return
            self._data[key] = value
            try:
                self._save()
            except OSError as exc:
                _LOG.error("Could not save the settings: %s", exc)
