"""Colors, stylesheet and icons drawn in code."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

BG = "#0f1115"
SURFACE = "#181b21"
RAISED = "#20242c"
BORDER = "#272b33"
BORDER_STRONG = "#333944"
TEXT = "#eef0f3"
MUTED = "#9aa1ad"
ACCENT = "#5b8cff"
ACCENT_TEXT = "#0b0d11"
OK = "#3ecf8e"
WARN = "#f0b429"
ERR = "#ff6b6b"

STYLESHEET = f"""
QWidget {{ color: {TEXT}; font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 10pt; }}
QWidget#root {{ background: {BG}; }}
QWidget#page, QWidget#scrollBody {{ background: transparent; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 15pt; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 10pt; font-weight: 600; color: {MUTED}; }}
QLabel[role="name"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="muted"] {{ color: {MUTED}; font-size: 9pt; }}
QLabel[role="error"] {{ color: {ERR}; font-size: 9pt; }}
QLabel[role="ok"] {{ color: {OK}; font-size: 9pt; }}
QLabel[role="pill"] {{
    background: {RAISED}; border: 1px solid {BORDER}; border-radius: 12px;
    padding: 3px 10px; font-size: 9pt; font-weight: 600;
}}
QFrame[role="card"] {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame[role="drop"] {{ background: {SURFACE}; border: 1px dashed {BORDER_STRONG}; border-radius: 12px; }}
QFrame[role="drop"][hot="true"] {{ border: 1px dashed {ACCENT}; background: #1a2030; }}
QFrame[role="row"] {{ background: {RAISED}; border: none; border-radius: 8px; }}
QFrame[role="line"] {{ background: {BORDER}; border: none; max-height: 1px; min-height: 1px; }}

QPushButton {{
    background: {RAISED}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 6px 14px; min-height: 20px;
}}
QPushButton:hover {{ background: #272c35; }}
QPushButton:pressed {{ background: #1b1f26; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5d6470; border-color: {BORDER}; }}
QPushButton[role="primary"] {{ background: {ACCENT}; color: {ACCENT_TEXT}; border: 1px solid {ACCENT}; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: #7aa2ff; }}
QPushButton[role="primary"]:focus {{ border-color: {TEXT}; }}
QPushButton[role="quiet"] {{ background: transparent; border: 1px solid transparent; color: {MUTED}; padding: 4px 8px; }}
QPushButton[role="quiet"]:hover {{ color: {TEXT}; background: {RAISED}; }}
QPushButton[role="quiet"]:focus {{ border-color: {ACCENT}; }}
QPushButton[role="tab"] {{
    background: transparent; border: 1px solid transparent; border-radius: 8px;
    padding: 7px 14px; color: {MUTED}; font-weight: 600;
}}
QPushButton[role="tab"]:hover {{ color: {TEXT}; }}
QPushButton[role="tab"]:checked {{ background: {RAISED}; color: {TEXT}; }}
QPushButton[role="tab"]:focus {{ border-color: {ACCENT}; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER_STRONG}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; background: {TEXT}; }}
QSlider::handle:horizontal:focus {{ background: {ACCENT}; border: 2px solid {TEXT}; }}
QSlider::handle:horizontal:disabled {{ background: #5d6470; }}
QSlider::sub-page:horizontal:disabled {{ background: #3a404b; }}

QComboBox {{
    background: {RAISED}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 6px 10px; min-height: 20px;
}}
QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {RAISED}; border: 1px solid {BORDER_STRONG}; outline: none;
    selection-background-color: {ACCENT}; selection-color: {ACCENT_TEXT};
}}

QDialog, QMessageBox, QInputDialog {{ background: {SURFACE}; }}
QLineEdit {{
    background: {BG}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 7px 10px; selection-background-color: {ACCENT}; selection-color: {ACCENT_TEXT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #454c59; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px 6px; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {RAISED}; }}
QMenu::item:disabled {{ color: #5d6470; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
"""


def _painter(pixmap: QPixmap) -> QPainter:
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return painter


def _draw_waves(painter: QPainter, size: float, color: QColor) -> None:
    """Broadcast waves + base: the app's own mark."""
    pen = QPen(color, size * 0.075, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    center = QPointF(size * 0.5, size * 0.60)
    for radius in (0.16, 0.29, 0.42):
        r = size * radius
        rect = QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r)
        painter.drawArc(rect, 35 * 16, 110 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    base = QPainterPath()
    base.moveTo(size * 0.5, size * 0.56)
    base.lineTo(size * 0.70, size * 0.80)
    base.lineTo(size * 0.30, size * 0.80)
    base.closeSubpath()
    painter.drawPath(base)


def app_pixmap(size: int = 256, streaming: bool = False) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = _painter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.23, size * 0.23)
    _draw_waves(painter, size, QColor("#ffffff"))
    if streaming:
        r = size * 0.17
        painter.setBrush(QColor(BG))
        painter.drawEllipse(QPointF(size - r * 1.05, r * 1.05), r * 1.2, r * 1.2)
        painter.setBrush(QColor(OK))
        painter.drawEllipse(QPointF(size - r * 1.05, r * 1.05), r * 0.85, r * 0.85)
    painter.end()
    return pixmap


def app_icon(streaming: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(size, streaming))
    return icon


def speaker_pixmap(pair: bool, active: bool, size: int = 40, dpr: float = 1.0) -> QPixmap:
    """Speaker row icon: one body or two."""
    pixmap = QPixmap(int(size * dpr), int(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = _painter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1d2740" if active else RAISED))
    painter.drawRoundedRect(QRectF(0, 0, size, size), 10, 10)
    body = QColor(ACCENT if active else MUTED)
    painter.setBrush(body)
    if pair:
        for cx in (size * 0.32, size * 0.68):
            painter.drawRoundedRect(QRectF(cx - size * 0.13, size * 0.30, size * 0.26, size * 0.40), 5, 5)
    else:
        painter.drawRoundedRect(QRectF(size * 0.33, size * 0.27, size * 0.34, size * 0.46), 6, 6)
    painter.end()
    return pixmap
