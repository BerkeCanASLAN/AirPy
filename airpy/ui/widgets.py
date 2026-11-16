"""Custom UI widgets."""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import theme


def set_role(widget: QWidget, role: str) -> None:
    """Apply the [role="..."] selector from the stylesheet."""
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def label(text: str = "", role: str = "") -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    return widget


def fmt_size(size: float) -> str:
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return (f"{size:.1f}" if size < 10 else f"{size:.0f}") + f" {unit}"
    return ""


class ElidedLabel(QLabel):
    """Label that shortens text that does not fit with '…'."""

    def __init__(self, text: str = "", role: str = "") -> None:
        super().__init__()
        self._full = ""
        if role:
            self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text if len(text) > 30 else "")
        self._refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, max(self.width(), 40)))


class Toggle(QAbstractButton):
    """Animated on/off switch (also usable with the keyboard)."""

    def __init__(self, accessible_name: str = "") -> None:
        super().__init__()
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(46, 26)
        if accessible_name:
            self.setAccessibleName(accessible_name)
        self._knob = 0.0
        self._animation = QPropertyAnimation(self, b"knob", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(46, 26)

    def _get_knob(self) -> float:
        return self._knob

    def _set_knob(self, value: float) -> None:
        self._knob = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        if not self.isVisible():
            self._set_knob(1.0 if checked else 0.0)
            return
        self._animation.setStartValue(self._knob)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def setCheckedSilently(self, checked: bool) -> None:
        if self.isChecked() == checked:
            return
        blocked = self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(blocked)
        self._animate(checked)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        off, on = QColor("#3a404b"), QColor(theme.ACCENT)
        t = self._knob
        track = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
        )
        if not self.isEnabled():
            track.setAlpha(110)
        rect = QRectF(2, 3, 42, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, 10, 10)
        x = 5 + t * 22
        painter.setBrush(QColor("#ffffff" if self.isEnabled() else "#9aa1ad"))
        painter.drawEllipse(QRectF(x, 6, 14, 14))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(theme.TEXT), 1.5))
            painter.drawRoundedRect(QRectF(0.75, 1.75, 44.5, 22.5), 11, 11)


class LevelMeter(QWidget):
    """Thin bar showing the peak level of the captured audio."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._value = 0.0

    def push(self, peak: float) -> None:
        # dB scale: -60 dB..0 dB -> 0..1; the fall-off is smoothed
        import math

        level = 0.0 if peak <= 1e-4 else max(0.0, min(1.0, 1.0 + math.log10(peak) / 3.0))
        self._value = level if level > self._value else self._value * 0.82
        if self._value < 0.005:
            self._value = 0.0
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.setBrush(QColor(theme.BORDER_STRONG))
        painter.drawRoundedRect(rect, 3, 3)
        if self._value > 0:
            fill = QRectF(0, 0, max(6.0, self.width() * self._value), self.height())
            painter.setBrush(QColor(theme.WARN if self._value > 0.97 else theme.OK))
            painter.drawRoundedRect(fill, 3, 3)


class TargetCard(QFrame):
    """One speaker / stereo pair row."""

    toggled = Signal(str, bool)
    volume_changed = Signal(str, float)
    pair_requested = Signal(str)

    _STATE_TEXT = {
        "connecting": ("Connecting…", "muted"),
        "streaming": ("Streaming PC audio", "ok"),
    }

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        self.setProperty("role", "card")
        self._data: dict = {}
        self._user_volume_at = 0.0

        self._icon = QLabel()
        self._icon.setFixedSize(40, 40)
        self._name = ElidedLabel("", "name")
        self._sub = ElidedLabel("", "muted")
        self._toggle = Toggle()
        self._toggle.toggled.connect(lambda on: self.toggled.emit(self.key, on))

        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(self._name)
        text.addWidget(self._sub)

        self._pair = QPushButton("Pair")
        self._pair.setProperty("role", "primary")
        self._pair.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pair.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._pair.clicked.connect(lambda: self.pair_requested.emit(self.key))
        self._pair.hide()

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self._icon)
        top.addLayout(text, 1)
        top.addWidget(self._pair)
        top.addWidget(self._toggle)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 100)
        self._slider.setPageStep(5)
        self._slider.setAccessibleName("Volume level")
        self._slider.valueChanged.connect(self._slider_moved)
        self._percent = label("", "muted")
        self._percent.setFixedWidth(40)
        self._percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._volume_row = QWidget()
        row = QHBoxLayout(self._volume_row)
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(10)
        row.addWidget(label("Volume", "muted"))
        row.addWidget(self._slider, 1)
        row.addWidget(self._percent)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(
            lambda: self.volume_changed.emit(self.key, float(self._slider.value()))
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addWidget(self._volume_row)
        self._volume_row.hide()

    def _slider_moved(self, value: int) -> None:
        import time

        self._percent.setText(f"{value}%")
        self._user_volume_at = time.monotonic()
        self._debounce.start()

    def update_data(self, data: dict) -> None:
        import time

        self._data = data
        pair = data["kind"] == "pair"
        active = bool(data["active"])
        state = data["state"]

        self._name.setFullText(data["name"])
        self._toggle.setCheckedSilently(active)
        self._toggle.setAccessibleName(f"Stream to {data['name']}")
        dpr = self.devicePixelRatioF()
        self._icon.setPixmap(theme.speaker_pixmap(pair, state == "streaming", 40, dpr))

        pairable = bool(data.get("pairable"))
        self._pair.setVisible(pairable)
        self._toggle.setVisible(not pairable)
        self._pair.setAccessibleName(f"Pair {data['name']}")

        model = data.get("model") or "AirPlay speaker"
        description = f"Stereo pair · 2 × {model}" if pair else model
        if pairable:
            text, role = f"{model} · pairing required", "muted"
        elif state == "error":
            text, role = data.get("message") or "Could not connect", "error"
            if not data.get("message", "").startswith("Access"):
                text += " · will retry"
        elif not data.get("online", True):
            text, role = data.get("message") or "Looking for it on the network…", "muted"
        elif state in self._STATE_TEXT:
            text, role = self._STATE_TEXT[state]
        else:
            text, role = description, "muted"
        self._sub.setFullText(text)
        if self._sub.property("role") != role:
            set_role(self._sub, role)
        self._sub.setToolTip(text)

        # Volume can be adjusted while not streaming too (the speaker's own level)
        volume = data.get("volume")
        show = volume is not None and not pairable and data.get("online", True)
        self._volume_row.setVisible(show)
        if show and not self._slider.isSliderDown() and time.monotonic() - self._user_volume_at > 1.5:
            blocked = self._slider.blockSignals(True)
            self._slider.setValue(int(round(volume)))
            self._slider.blockSignals(blocked)
            self._percent.setText(f"{int(round(volume))}%")


class QrLabel(QLabel):
    """Draws a QR code for the given address (white background, quiet zone included)."""

    def __init__(self, size: int = 168) -> None:
        super().__init__()
        self._size = size
        self.setFixedSize(size, size)
        self.setAccessibleName("Phone connection QR code")

    def set_url(self, url: str) -> None:
        if not url:
            self.clear()
            return
        import qrcode

        code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        code.add_data(url)
        code.make(fit=True)
        matrix = code.get_matrix()
        dpr = self.devicePixelRatioF()
        pixels = int(self._size * dpr)
        pixmap = QPixmap(pixels, pixels)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(QRectF(0, 0, pixels, pixels), 10 * dpr, 10 * dpr)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        count = len(matrix)
        cell = pixels // count
        offset = (pixels - cell * count) // 2
        painter.setBrush(QColor("#000000"))
        for y, line in enumerate(matrix):
            for x, dark in enumerate(line):
                if dark:
                    painter.drawRect(offset + x * cell, offset + y * cell, cell, cell)
        painter.end()
        pixmap.setDevicePixelRatio(dpr)
        self.setPixmap(pixmap)


class DropFrame(QFrame):
    """Area where files can be dropped."""

    files_dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("role", "drop")
        self.setAcceptDrops(True)

    def _set_hot(self, hot: bool) -> None:
        self.setProperty("hot", "true" if hot else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_hot(True)

    def dragLeaveEvent(self, event) -> None:
        self._set_hot(False)

    def dropEvent(self, event) -> None:
        self._set_hot(False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


def hline() -> QFrame:
    line = QFrame()
    line.setProperty("role", "line")
    return line


def setting_row(title: str, description: str, control: QWidget, below: bool = False) -> QWidget:
    """Setting row: title + description; the control goes on the right or (for wide controls) below."""
    row = QWidget()
    text = QVBoxLayout()
    text.setSpacing(1)
    text.addWidget(label(title))
    if description:
        desc = label(description, "muted")
        desc.setWordWrap(True)
        text.addWidget(desc)
    if below:
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(text)
        layout.addWidget(control)
        return row
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    layout.addLayout(text, 1)
    layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return row
