"""Application entry: Qt app, system tray and single-instance check."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import APP_NAME, autostart
from .config import Config, app_dir
from .engine import Engine
from .ui import theme
from .ui.window import MainWindow

_LOG = logging.getLogger(__name__)


def _setup_logging() -> None:
    handler = RotatingFileHandler(
        app_dir() / "airpy.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("pyatv").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    def excepthook(exc_type, exc, tb):
        _LOG.critical("Unexpected error", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook


def _instance_name() -> str:
    """Channel name for one instance per user (and profile folder)."""
    seed = f"{getpass.getuser()}|{app_dir()}".encode("utf-8", "replace")
    return f"{APP_NAME}-{hashlib.sha1(seed).hexdigest()[:12]}"


def _notify_running_instance(name: str, command: bytes = b"show") -> bool:
    """Send a command to a running instance, if any ("show": open the window, "quit": exit)."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(300):
        return False
    socket.write(command)
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def _quit_running_instance(name: str) -> int:
    """Close the running instance cleanly (ends the streams, unmutes the PC speakers)."""
    import time

    if not _notify_running_instance(name, b"quit"):
        print("AirPy is not running.")
        return 0
    for _ in range(40):
        time.sleep(0.25)
        probe = QLocalSocket()
        probe.connectToServer(name)
        if not probe.waitForConnected(150):
            print("AirPy was closed.")
            return 0
        probe.disconnectFromServer()
    print("AirPy did not close in time.")
    return 1


class Bridge(QObject):
    """Carries events from the engine thread to the UI thread."""

    event = Signal(str, object)

    def emit_event(self, name: str, payload: dict) -> None:
        self.event.emit(name, payload)


class Tray(QObject):
    def __init__(self, app: QApplication, window: MainWindow, engine: Engine) -> None:
        super().__init__()
        self._app = app
        self._window = window
        self._engine = engine
        self._streaming = False
        self._errors: set[str] = set()
        self._hint_shown = False
        self._icons = {False: theme.app_icon(False), True: theme.app_icon(True)}

        self._tray = QSystemTrayIcon(self._icons[False])
        self._tray.setToolTip(APP_NAME)
        self._menu = QMenu()
        self._show_action = QAction("Show AirPy")
        self._show_action.triggered.connect(window.bring_to_front)
        self._quit_action = QAction("Quit")
        self._quit_action.triggered.connect(self.quit)
        self._target_actions: list[QAction] = []
        self._menu_signature: object = None
        self._rebuild_menu([])
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._activated)
        self._tray.show()
        window.hidden_to_tray.connect(self._on_hidden)

    def _rebuild_menu(self, rows: list[dict]) -> None:
        signature = [(r["key"], r["name"], bool(r["active"])) for r in rows]
        if signature == self._menu_signature:
            return  # do not rebuild an open menu needlessly
        self._menu_signature = signature
        self._menu.clear()
        self._menu.addAction(self._show_action)
        self._menu.addSeparator()
        self._target_actions = []
        for row in rows:
            action = QAction(row["name"])
            action.setCheckable(True)
            action.setChecked(bool(row["active"]))
            action.triggered.connect(
                lambda checked, key=row["key"]: self._engine.set_target_active(key, checked)
            )
            self._menu.addAction(action)
            self._target_actions.append(action)
        if not rows:
            empty = QAction("No speakers found")
            empty.setEnabled(False)
            self._menu.addAction(empty)
            self._target_actions.append(empty)
        self._menu.addSeparator()
        self._menu.addAction(self._quit_action)

    def _activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self._window.isVisible() and not self._window.isMinimized():
                self._window.hide()
            else:
                self._window.bring_to_front()

    def _on_hidden(self) -> None:
        if not self._hint_shown:
            self._hint_shown = True
            self._message("AirPy keeps running", "Click the tray icon to open the window.")

    def _message(self, title: str, text: str) -> None:
        self._tray.showMessage(title, text, self._icons[False], 4000)

    def on_event(self, event: str, payload: dict) -> None:
        if event == "targets":
            rows = payload["targets"]
            self._rebuild_menu(rows)
            streaming = [r["name"] for r in rows if r["active"] and r["state"] == "streaming"]
            if bool(streaming) != self._streaming:
                self._streaming = bool(streaming)
                self._tray.setIcon(self._icons[self._streaming])
            self._tray.setToolTip(
                f"{APP_NAME} · streaming to {', '.join(streaming)}" if streaming else APP_NAME
            )
            errors = {r["key"] for r in rows if r["active"] and r["state"] == "error"}
            for row in rows:
                if row["key"] in errors - self._errors and not self._window.isVisible():
                    self._message(f"Could not connect to {row['name']}", row.get("message") or "")
            self._errors = errors
        elif event == "received":
            self._message("File received", payload["name"])
        elif event == "text":
            QGuiApplication.clipboard().setText(payload["text"])
            preview = payload["text"].strip().replace("\n", " ")
            self._message("Text from your phone was copied to the clipboard", preview[:120])
        elif event == "notice":
            self._message(APP_NAME, payload.get("text", ""))

    def quit(self) -> None:
        self._window.allow_quit()
        self._tray.hide()
        self._app.quit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--hidden", action="store_true", help="start in the tray without opening a window")
    parser.add_argument("--quit", action="store_true", help="quit a running AirPy cleanly")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    _setup_logging()
    try:  # show our own icon in the taskbar instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AirPy.App")
    except Exception:
        pass

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(theme.app_icon())
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(theme.STYLESHEET)

    name = _instance_name()
    if args.quit:
        return _quit_running_instance(name)
    if _notify_running_instance(name):
        return 0
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.listen(name)

    config = Config()
    # A test/development profile (AIRPY_HOME) does not touch the registry.
    manage_autostart = "AIRPY_HOME" not in os.environ
    bridge = Bridge()
    engine = Engine(config, bridge.emit_event)
    window = MainWindow(config, engine, manage_autostart=manage_autostart)
    bridge.event.connect(window.on_event)

    # At sign-in the tray can be ready a few seconds late; wait until it is.
    state: dict = {"tray": None, "tries": 0}
    tray_timer = QTimer()
    tray_timer.setInterval(2000)

    def ensure_tray() -> None:
        if state["tray"] is None and QSystemTrayIcon.isSystemTrayAvailable():
            state["tray"] = Tray(app, window, engine)
            bridge.event.connect(state["tray"].on_event)
            engine.rescan()  # so the tray menu gets the current target list
        state["tries"] += 1
        if state["tray"] is not None:
            tray_timer.stop()
        elif state["tries"] >= 30:
            # No tray: the window is the only way in, so closing it quits the app.
            tray_timer.stop()
            window.allow_quit()
            app.setQuitOnLastWindowClosed(True)
            window.show()

    tray_timer.timeout.connect(ensure_tray)
    ensure_tray()
    if state["tray"] is None:
        tray_timer.start()

    def request_quit() -> None:
        if state["tray"] is not None:
            state["tray"].quit()
        else:
            window.allow_quit()
            app.quit()

    def on_command(connection) -> None:
        command = bytes(connection.readAll().data()).strip()
        if command == b"quit":
            request_quit()
        elif command == b"show":
            window.bring_to_front()

    def on_new_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return
        connection.readyRead.connect(lambda c=connection: on_command(c))
        connection.disconnected.connect(connection.deleteLater)
        if connection.bytesAvailable():
            on_command(connection)

    server.newConnection.connect(on_new_connection)

    if manage_autostart:
        autostart.sync(bool(config.get("autostart")))

    engine.start()
    app.aboutToQuit.connect(engine.shutdown)
    if not args.hidden:
        window.show()
    _LOG.info("AirPy started (hidden=%s)", args.hidden)
    code = app.exec()
    server.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
