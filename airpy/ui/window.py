"""Main window: Speakers, Sharing and Settings pages."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__, airplay, autostart
from ..config import Config
from . import theme
from .widgets import (
    DropFrame,
    ElidedLabel,
    LevelMeter,
    QrLabel,
    TargetCard,
    Toggle,
    fmt_size,
    hline,
    label,
    setting_row,
)

_PAIR_MODES = [
    ("leader", "Leader only (recommended)"),
    ("both", "Separate stream to both speakers"),
]
_SHARE_HINTS = {
    "tailscale": (
        "Scan the QR code with your phone's camera. With Tailscale running on the phone, "
        "the same address works at home, away and on mobile data; files and text go both ways."
    ),
    "lan": (
        "Scan the QR code with your phone's camera. The page that opens lets you send files and text "
        "to the PC and download what you add from it. Both devices must be on the same network."
    ),
}


def _button(text: str, role: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    # Show the focus ring only when navigating with the keyboard (mouse clicks do not leave focus)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    if role:
        button.setProperty("role", role)
    return button


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("role", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(10)
    return frame, layout


def _scroll(body: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setFrameShape(QFrame.Shape.NoFrame)
    body.setObjectName("scrollBody")
    area.setWidget(body)
    area.viewport().setAutoFillBackground(False)
    return area


class MainWindow(QWidget):
    """Application window. Closing it keeps the app running in the tray."""

    hidden_to_tray = Signal()

    def __init__(self, config: Config, engine, manage_autostart: bool = True) -> None:
        super().__init__()
        self.config = config
        self.engine = engine
        self._manage_autostart = manage_autostart
        self._cards: dict[str, TargetCard] = {}
        self._received: list[dict] = []
        self._share_url = ""
        self._local_port = 0
        self._streaming = False
        self._quitting = False

        self.setObjectName("root")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.resize(460, 700)
        self.setMinimumSize(400, 520)
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)
        root.addLayout(self._build_header())
        root.addLayout(self._build_tabs())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_speakers_page())
        self._stack.addWidget(self._build_share_page())
        self._stack.addWidget(self._build_settings_page())
        root.addWidget(self._stack, 1)

        # Keep the first tab from showing a focus ring on startup; the Tab key still works.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setFocus()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_header(self) -> QHBoxLayout:
        icon = QLabel()
        dpr = self.devicePixelRatioF()
        pixmap = theme.app_pixmap(int(32 * dpr))
        pixmap.setDevicePixelRatio(dpr)
        icon.setPixmap(pixmap)
        title = label(APP_NAME, "title")
        self._pill = label("Ready", "pill")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(icon)
        row.addWidget(title)
        row.addStretch(1)
        row.addWidget(self._pill)
        return row

    def _build_tabs(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        for index, text in enumerate(("Speakers", "Sharing", "Settings")):
            tab = _button(text, "tab")
            tab.setCheckable(True)
            tab.setChecked(index == 0)
            self._tab_group.addButton(tab, index)
            row.addWidget(tab)
        row.addStretch(1)
        self._tab_group.idClicked.connect(lambda index: self._stack.setCurrentIndex(index))
        return row

    def show_tab(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._tab_group.button(index).setChecked(True)

    # -- Speakers ------------------------------------------------------
    def _build_speakers_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(label("AirPlay speakers", "heading"))
        head.addStretch(1)
        self._rescan = _button("Refresh", "quiet")
        self._rescan.clicked.connect(self._on_rescan)
        head.addWidget(self._rescan)
        layout.addLayout(head)

        body = QWidget()
        self._targets_layout = QVBoxLayout(body)
        self._targets_layout.setContentsMargins(0, 0, 0, 0)
        self._targets_layout.setSpacing(10)

        self._empty, empty_layout = _card()
        self._empty_title = label("Looking for speakers…")
        self._empty_text = label(
            "HomePods must be on and on the same network as this PC. If they do not show up, in the Home app set "
            "Home Settings › Speakers & TV › Allow Speaker & TV Access to “Anyone on the Same Network”.",
            "muted",
        )
        self._empty_text.setWordWrap(True)
        empty_layout.addWidget(self._empty_title)
        empty_layout.addWidget(self._empty_text)
        self._targets_layout.addWidget(self._empty)
        self._targets_layout.addStretch(1)
        layout.addWidget(_scroll(body), 1)

        self._source_card, source_layout = _card()
        source_layout.setSpacing(8)
        self._source_label = ElidedLabel("", "muted")
        self._meter = LevelMeter()
        source_layout.addWidget(self._source_label)
        source_layout.addWidget(self._meter)
        self._source_card.hide()
        layout.addWidget(self._source_card)
        return page

    # -- Sharing ---------------------------------------------------------
    def _build_share_page(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(10)

        # Connection / QR
        qr_card, qr_layout = _card()
        row = QHBoxLayout()
        row.setSpacing(14)
        self._qr = QrLabel(148)
        row.addWidget(self._qr, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(6)
        text.addWidget(label("Connect your phone", "name"))
        # Address choice: Tailscale (works from any network) / local network. Hidden without Tailscale.
        self._via_row = QWidget()
        via_layout = QHBoxLayout(self._via_row)
        via_layout.setContentsMargins(0, 0, 0, 0)
        via_layout.setSpacing(4)
        self._via_group = QButtonGroup(self)
        self._via_group.setExclusive(True)
        self._via_buttons: dict[str, QPushButton] = {}
        for key, caption in (("tailscale", "Tailscale"), ("lan", "Local network")):
            option = _button(caption, "tab")
            option.setCheckable(True)
            option.clicked.connect(lambda _=False, k=key: self.engine.set_option("share_via", k))
            self._via_group.addButton(option)
            self._via_buttons[key] = option
            via_layout.addWidget(option)
        via_layout.addStretch(1)
        self._via_row.hide()
        text.addWidget(self._via_row)
        self._share_hint = label(_SHARE_HINTS["lan"], "muted")
        self._share_hint.setWordWrap(True)
        text.addWidget(self._share_hint)
        self._url_label = ElidedLabel("", "muted")
        self._url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self._url_label)
        self._copy_url = _button("Copy link")
        self._copy_url.clicked.connect(self._on_copy_url)
        text.addWidget(self._copy_url, 0, Qt.AlignmentFlag.AlignLeft)
        text.addStretch(1)
        row.addLayout(text, 1)
        qr_layout.addLayout(row)
        layout.addWidget(qr_card)

        # PC -> phone
        layout.addWidget(label("Send to phone", "heading"))
        self._drop = DropFrame()
        self._drop.files_dropped.connect(self._share_paths)
        drop_layout = QVBoxLayout(self._drop)
        drop_layout.setContentsMargins(14, 14, 14, 14)
        drop_layout.setSpacing(10)
        drop_text = label("Drag files here; they become downloadable on the phone page.", "muted")
        drop_text.setWordWrap(True)
        drop_layout.addWidget(drop_text)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        pick = _button("Choose files…", "primary")
        pick.clicked.connect(self._on_pick_files)
        paste = _button("Send clipboard text")
        paste.clicked.connect(self._on_share_clipboard)
        buttons.addWidget(pick)
        buttons.addWidget(paste)
        buttons.addStretch(1)
        self._clear_items = _button("Clear", "quiet")
        self._clear_items.clicked.connect(self.engine.clear_share_items)
        self._clear_items.hide()
        buttons.addWidget(self._clear_items)
        drop_layout.addLayout(buttons)
        self._items_layout = QVBoxLayout()
        self._items_layout.setSpacing(6)
        drop_layout.addLayout(self._items_layout)
        layout.addWidget(self._drop)

        # phone -> PC
        head = QHBoxLayout()
        head.addWidget(label("Received from phone", "heading"))
        head.addStretch(1)
        open_dir = _button("Open folder", "quiet")
        open_dir.clicked.connect(self._on_open_receive_dir)
        head.addWidget(open_dir)
        layout.addLayout(head)
        received_card, received_layout = _card()
        self._received_empty = label("No files received yet.", "muted")
        received_layout.addWidget(self._received_empty)
        self._received_layout = QVBoxLayout()
        self._received_layout.setSpacing(6)
        received_layout.addLayout(self._received_layout)
        layout.addWidget(received_card)
        layout.addStretch(1)

        page = _scroll(body)
        page.setObjectName("page")
        return page

    # -- Settings ----------------------------------------------------------
    def _build_settings_page(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(10)

        layout.addWidget(label("Startup", "heading"))
        card, card_layout = _card()
        self._opt_autostart = self._toggle_setting(
            card_layout, "autostart", "Start with Windows",
            "Runs in the system tray without showing a window when you sign in.",
        )
        card_layout.addWidget(hline())
        self._opt_auto_connect = self._toggle_setting(
            card_layout, "auto_connect", "Auto-connect to selected speakers",
            "Starts streaming to the speakers you used last when the app opens.",
        )
        card_layout.addWidget(hline())
        self._make_shortcut = _button("Create")
        self._make_shortcut.clicked.connect(self._on_make_shortcut)
        card_layout.addWidget(
            setting_row(
                "Desktop shortcut",
                "Adds an AirPy icon to the desktop (updates it if it exists).",
                self._make_shortcut,
            )
        )
        layout.addWidget(card)

        layout.addWidget(label("Audio", "heading"))
        card, card_layout = _card()
        self._opt_mute = self._toggle_setting(
            card_layout, "mute_local", "Mute this PC's speakers",
            "While streaming, sound comes only from the AirPlay speakers; it is restored when streaming stops.",
        )
        card_layout.addWidget(hline())
        self._pair_mode = self._combo_setting(
            card_layout, "pair_mode", _PAIR_MODES, "Stereo pair",
            "If sound comes from one speaker only or left/right are swapped, try the other mode.",
        )
        card_layout.addWidget(hline())
        # Latency: speaker buffer (ms). The stream restarts when the slider is released.
        self._latency = QSlider(Qt.Orientation.Horizontal)
        self._latency.setAccessibleName("Speaker buffer")
        self._latency.setRange(airplay.LATENCY_MIN_MS // 10, 150)  # 50-1500 ms in 10 ms steps
        self._latency.setSingleStep(1)
        self._latency.setPageStep(5)
        self._latency.setValue(max(5, min(150, int(self.config.get("latency_ms")) // 10)))
        self._latency_text = label("", "muted")
        self._latency_apply = QTimer(self)
        self._latency_apply.setSingleShot(True)
        self._latency_apply.setInterval(700)
        self._latency_apply.timeout.connect(
            lambda: self.engine.set_option("latency_ms", self._latency.value() * 10)
        )
        self._latency.valueChanged.connect(self._on_latency_moved)
        latency_box = QWidget()
        latency_layout = QVBoxLayout(latency_box)
        latency_layout.setContentsMargins(0, 0, 0, 0)
        latency_layout.setSpacing(6)
        latency_layout.addWidget(self._latency)
        latency_layout.addWidget(self._latency_text)
        card_layout.addWidget(
            setting_row(
                "Latency",
                "How long the sound waits in the speaker. Drag left; if it crackles or drops out, "
                "move it back right. Below 250 ms is experimental and depends on your network.",
                latency_box,
                below=True,
            )
        )
        self._on_latency_moved(self._latency.value(), apply=False)
        card_layout.addWidget(hline())
        self._test_tone = _button("Play left / right test tone")
        self._test_tone.clicked.connect(self.engine.play_test_tone)
        self._test_tone.setEnabled(False)
        card_layout.addWidget(
            setting_row(
                "Speaker test",
                "Plays a short tone on the left, then the right, then both. Available while streaming.",
                self._test_tone,
                below=True,
            )
        )
        layout.addWidget(card)

        layout.addWidget(label("File sharing", "heading"))
        card, card_layout = _card()
        self._opt_share = self._toggle_setting(
            card_layout, "share_enabled", "Phone file sharing",
            "When off, the phone page does not work.",
        )
        card_layout.addWidget(hline())
        self._receive_dir_label = ElidedLabel(self.config.get("receive_dir"), "muted")
        change = _button("Change…")
        change.clicked.connect(self._on_change_receive_dir)
        folder_row = QHBoxLayout()
        folder_text = QVBoxLayout()
        folder_text.setSpacing(1)
        folder_text.addWidget(label("Received files folder"))
        folder_text.addWidget(self._receive_dir_label)
        folder_row.addLayout(folder_text, 1)
        folder_row.addWidget(change)
        card_layout.addLayout(folder_row)
        layout.addWidget(card)

        layout.addWidget(label("Browser extension (Edge)", "heading"))
        card, card_layout = _card()
        open_folder = _button("Open folder")
        open_folder.clicked.connect(self._on_open_extension_dir)
        card_layout.addWidget(
            setting_row(
                "AirPy Sync extension",
                "Delays the video by the speaker latency so picture and sound stay in sync. "
                "Install: edge://extensions › Developer mode › Load unpacked › this folder.",
                open_folder,
                below=True,
            )
        )
        card_layout.addWidget(hline())
        self._open_calibration = _button("Open sync test")
        self._open_calibration.setEnabled(False)
        self._open_calibration.clicked.connect(self._on_open_calibration)
        card_layout.addWidget(
            setting_row(
                "Sync test",
                "Compare a light flash with a beep to fine-tune the extension. "
                "Use it while streaming, with the extension installed.",
                self._open_calibration,
                below=True,
            )
        )
        layout.addWidget(card)

        about = label(f"{APP_NAME} {__version__} · AirPlay streaming is provided by the pyatv library", "muted")
        about.setWordWrap(True)
        layout.addWidget(about)
        layout.addStretch(1)

        page = _scroll(body)
        page.setObjectName("page")
        return page

    def _toggle_setting(self, layout: QVBoxLayout, key: str, title: str, description: str) -> Toggle:
        toggle = Toggle(title)
        toggle.setChecked(bool(self.config.get(key)))
        toggle.toggled.connect(lambda on, k=key: self._on_option(k, on))
        layout.addWidget(setting_row(title, description, toggle))
        return toggle

    def _combo_setting(
        self, layout: QVBoxLayout, key: str, options: list, title: str, description: str
    ) -> QComboBox:
        combo = QComboBox()
        combo.setAccessibleName(title)
        for value, text in options:
            combo.addItem(text, value)
        index = max(0, combo.findData(self.config.get(key)))
        combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(
            lambda _i, k=key, c=combo: self._on_option(k, c.currentData())
        )
        layout.addWidget(setting_row(title, description, combo, below=True))
        return combo

    # ------------------------------------------------------------------
    # Engine events
    # ------------------------------------------------------------------
    def on_event(self, event: str, payload: dict) -> None:
        if event == "targets":
            self._update_targets(payload)
        elif event == "level":
            self._meter.push(float(payload.get("peak", 0.0)))
        elif event == "share":
            self._update_share(payload)
        elif event == "received":
            self._add_received(payload)
        elif event == "pair":
            self._on_pair(payload)
        elif event == "localapi":
            self._local_port = int(payload.get("port", 0))
            self._open_calibration.setEnabled(self._local_port > 0)

    def _on_open_extension_dir(self) -> None:
        folder = Path(__file__).resolve().parents[2] / "extension"
        try:
            os.startfile(str(folder))  # noqa: S606 - the project's own folder
        except OSError:
            pass

    def _on_open_calibration(self) -> None:
        if not self._local_port:
            return
        # The extension is installed in Edge; open the test there even if another browser is the default
        url = f"http://127.0.0.1:{self._local_port}/airpy/calibrate"
        try:
            os.startfile(f"microsoft-edge:{url}")  # noqa: S606
        except OSError:
            os.startfile(url)  # noqa: S606

    def _on_pair(self, payload: dict) -> None:
        """Pairing steps: ask for the code, report the result."""
        name, stage = payload.get("name", ""), payload.get("stage")
        if stage == "pin":
            self.bring_to_front()
            dialog = QInputDialog(self)
            dialog.setWindowTitle("Pairing")
            dialog.setLabelText(f"Enter the code shown on {name}:")
            dialog.setTextEchoMode(QLineEdit.EchoMode.Normal)
            dialog.setOkButtonText("Pair")
            dialog.setCancelButtonText("Cancel")
            ok = dialog.exec() == QInputDialog.DialogCode.Accepted
            pin = dialog.textValue()
            if ok and pin.strip():
                self.engine.pair_submit(payload["key"], pin.strip())
            else:
                self.engine.pair_cancel()
        elif stage == "error":
            QMessageBox.warning(
                self, "Pairing failed",
                f"Could not pair with {name}.\n\n{payload.get('message', '')}",
            )
        elif stage == "done":
            QMessageBox.information(
                self, "Pairing complete", f"{name} is paired. You can now switch it on."
            )

    def _update_targets(self, payload: dict) -> None:
        rows = payload["targets"]
        keys = [row["key"] for row in rows]
        for key in list(self._cards):
            if key not in keys:
                card = self._cards.pop(key)
                self._targets_layout.removeWidget(card)
                card.deleteLater()
        for index, row in enumerate(rows):
            card = self._cards.get(row["key"])
            if card is None:
                card = TargetCard(row["key"])
                card.toggled.connect(self.engine.set_target_active)
                card.volume_changed.connect(self.engine.set_volume)
                card.pair_requested.connect(self.engine.pair_begin)
                self._cards[row["key"]] = card
                self._targets_layout.insertWidget(index, card)
            card.update_data(row)

        scanning = bool(payload.get("scanning"))
        self._rescan.setText("Scanning…" if scanning else "Refresh")
        self._rescan.setEnabled(not scanning)
        self._empty.setVisible(not rows)
        self._empty_title.setText("Looking for speakers…" if scanning else "No speakers found")

        states = [row["state"] for row in rows if row["active"]]
        self._streaming = "streaming" in states
        if "error" in states:
            text, color = "Problem", theme.ERR
        elif self._streaming and "connecting" not in states:
            text, color = "Streaming", theme.OK
        elif states:
            text, color = "Connecting…", theme.WARN
        else:
            text, color = "Ready", theme.MUTED
        self._pill.setText(text)
        self._pill.setStyleSheet(f"color: {color};")
        self._test_tone.setEnabled(self._streaming)

        capture = payload.get("capture") or ""
        error = payload.get("capture_error") or ""
        self._source_card.setVisible(bool(states) and bool(capture or error))
        if error and not capture:
            self._source_label.setFullText(f"Cannot capture PC audio: {error}")
        else:
            self._source_label.setFullText(f"Source: {capture}")

    def _update_share(self, payload: dict) -> None:
        running = bool(payload.get("running"))
        self._share_url = payload.get("url", "") if running else ""
        self._qr.set_url(self._share_url)
        via = payload.get("via", "lan")
        self._via_row.setVisible(running and bool(payload.get("has_tailscale")))
        if via in self._via_buttons:
            self._via_buttons[via].setChecked(True)
        self._share_hint.setText(_SHARE_HINTS.get(via, _SHARE_HINTS["lan"]))
        self._url_label.setFullText(self._share_url or "File sharing is off (turn it on in Settings)")
        self._copy_url.setEnabled(running)
        self._drop.setEnabled(running)
        self._receive_dir_label.setFullText(payload.get("receive_dir", ""))

        while self._items_layout.count():
            item = self._items_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        items = payload.get("items", [])
        self._clear_items.setVisible(bool(items))
        for entry in items:
            row = QFrame()
            row.setProperty("role", "row")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 6, 6, 6)
            name = entry["name"] if entry["kind"] == "file" else f"“{entry['name']}”"
            row_layout.addWidget(ElidedLabel(name), 1)
            detail = fmt_size(entry["size"]) if entry["kind"] == "file" else "text"
            row_layout.addWidget(label(detail, "muted"))
            remove = _button("Remove", "quiet")
            remove.clicked.connect(lambda _=False, i=entry["id"]: self.engine.remove_share_item(i))
            row_layout.addWidget(remove)
            self._items_layout.addWidget(row)

    def _add_received(self, info: dict) -> None:
        self._received.insert(0, info)
        del self._received[30:]
        while self._received_layout.count():
            item = self._received_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._received_empty.setVisible(not self._received)
        for entry in self._received:
            row = QFrame()
            row.setProperty("role", "row")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 6, 6, 6)
            row_layout.addWidget(ElidedLabel(entry["name"]), 1)
            row_layout.addWidget(label(fmt_size(entry["size"]), "muted"))
            show = _button("Show", "quiet")
            show.clicked.connect(lambda _=False, p=entry["path"]: self._reveal(p))
            row_layout.addWidget(show)
            self._received_layout.addWidget(row)

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------
    def _on_rescan(self) -> None:
        self._rescan.setText("Scanning…")
        self._rescan.setEnabled(False)
        self.engine.rescan()

    def _on_option(self, key: str, value) -> None:
        if key == "autostart":
            self.config.set(key, bool(value))
            if self._manage_autostart and not autostart.sync(bool(value)):
                self._opt_autostart.setCheckedSilently(autostart.is_enabled())
            return
        self.engine.set_option(key, value)

    def _on_latency_moved(self, value: int, apply: bool = True) -> None:
        ms = value * 10
        self._latency_text.setText(
            f"Buffer {ms} ms · total latency about {airplay.total_latency_ms(ms)} ms"
        )
        if apply:
            self._latency_apply.start()

    def _on_make_shortcut(self) -> None:
        from PySide6.QtCore import QTimer

        from .. import shortcut

        try:
            shortcut.create()
            text = "Created"
        except Exception as exc:
            text = "Failed"
            QMessageBox.warning(self, "Desktop shortcut", f"Could not create the shortcut.\n\n{exc}")
        self._make_shortcut.setText(text)
        QTimer.singleShot(2000, lambda: self._make_shortcut.setText("Create"))

    def _on_copy_url(self) -> None:
        if self._share_url:
            QGuiApplication.clipboard().setText(self._share_url)
            self._copy_url.setText("Copied")
            from PySide6.QtCore import QTimer

            QTimer.singleShot(1500, lambda: self._copy_url.setText("Copy link"))

    def _share_paths(self, paths: list) -> None:
        files = [p for p in paths if Path(p).is_file()]
        if files:
            self.engine.share_files(files)
            self.show_tab(1)

    def _on_pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Files to send to the phone")
        self._share_paths(files)

    def _on_share_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text.strip():
            self.engine.share_text(text)

    def _on_open_receive_dir(self) -> None:
        directory = Path(self.config.get("receive_dir"))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))  # noqa: S606 - the user's own folder
        except OSError:
            pass

    def _on_change_receive_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Received files folder", self.config.get("receive_dir")
        )
        if chosen:
            self.engine.set_option("receive_dir", str(Path(chosen)))
            self._receive_dir_label.setFullText(str(Path(chosen)))

    @staticmethod
    def _reveal(path: str) -> None:
        if Path(path).exists():
            subprocess.Popen(["explorer", "/select,", str(Path(path))])

    # ------------------------------------------------------------------
    # Window behavior
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self._share_paths(paths)

    def bring_to_front(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def allow_quit(self) -> None:
        self._quitting = True

    def closeEvent(self, event) -> None:
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.hidden_to_tray.emit()
