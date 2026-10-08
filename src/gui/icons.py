"""Small monochrome line icons drawn with Qt, without image-file dependencies.

Call ``make_icon`` after creating the QApplication. Icons use one 24-unit
coordinate system and include normal and disabled pixmaps at 1x and 2x.
"""

from __future__ import annotations

from functools import lru_cache
from math import cos, pi, sin

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox, QSpinBox, QStyle, QStyleOptionComboBox, QStyleOptionSpinBox


_ALIASES = {
    "folder_open": "folder-open",
    "volume_muted": "volume-muted",
    "exit_fullscreen": "exit-fullscreen",
    "chevron_down": "chevron-down",
    "chevron_up": "chevron-up",
    "chevron_left": "chevron-left",
    "chevron_right": "chevron-right",
    "info": "about",
    "browser": "globe",
    "scrape": "search",
}
_NAMES = {
    "link", "search", "globe", "folder-open", "folder", "stop", "play",
    "pause", "volume", "volume-muted", "fullscreen", "exit-fullscreen",
    "settings", "about", "more", "chevron-down", "chevron-up",
    "chevron-left", "chevron-right", "sun", "moon", "download", "close", "check",
}


def _line(painter: QPainter, x1: float, y1: float, x2: float, y2: float) -> None:
    painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _path(painter: QPainter, points: tuple[tuple[float, float], ...], *, closed: bool = False) -> None:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    if closed:
        path.closeSubpath()
    painter.drawPath(path)


def _draw(painter: QPainter, name: str) -> None:
    if name == "play":
        _path(painter, ((7, 4.5), (19, 12), (7, 19.5)), closed=True)
    elif name == "pause":
        painter.drawRoundedRect(QRectF(6, 5, 3, 14), 0.8, 0.8)
        painter.drawRoundedRect(QRectF(15, 5, 3, 14), 0.8, 0.8)
    elif name == "stop":
        painter.drawRoundedRect(QRectF(5.5, 5.5, 13, 13), 1.3, 1.3)
    elif name in {"volume", "volume-muted"}:
        _path(painter, ((3, 9), (7, 9), (11, 5.5), (11, 18.5), (7, 15), (3, 15)), closed=True)
        if name == "volume":
            path = QPainterPath(QPointF(15, 8))
            path.cubicTo(18.5, 10, 18.5, 14, 15, 16)
            painter.drawPath(path)
            path = QPainterPath(QPointF(18, 4.5))
            path.cubicTo(23.5, 8, 23.5, 16, 18, 19.5)
            painter.drawPath(path)
        else:
            _line(painter, 15.5, 9, 21.5, 15)
            _line(painter, 21.5, 9, 15.5, 15)
    elif name in {"fullscreen", "exit-fullscreen"}:
        corners = (
            ((4, 9), (4, 4), (9, 4)), ((15, 4), (20, 4), (20, 9)),
            ((20, 15), (20, 20), (15, 20)), ((9, 20), (4, 20), (4, 15)),
        ) if name == "fullscreen" else (
            ((4, 9), (9, 9), (9, 4)), ((15, 4), (15, 9), (20, 9)),
            ((20, 15), (15, 15), (15, 20)), ((9, 20), (9, 15), (4, 15)),
        )
        for corner in corners:
            _path(painter, corner)
    elif name in {"folder", "folder-open"}:
        _path(painter, ((3, 18.5), (3, 5.5), (9, 5.5), (11, 8), (21, 8), (21, 18.5), (3, 18.5)))
        if name == "folder-open":
            _path(painter, ((3, 18.5), (6, 11), (22, 11), (19, 18.5)), closed=True)
    elif name == "search":
        painter.drawEllipse(QRectF(3.5, 3.5, 12.5, 12.5))
        _line(painter, 14.5, 14.5, 20.5, 20.5)
    elif name == "globe":
        painter.drawEllipse(QRectF(3, 3, 18, 18))
        painter.drawEllipse(QRectF(7.5, 3, 9, 18))
        _line(painter, 3, 12, 21, 12)
    elif name == "link":
        painter.save()
        painter.translate(12, 12)
        painter.rotate(-45)
        painter.drawRoundedRect(QRectF(-3.5, -10, 7, 12), 3.5, 3.5)
        painter.drawRoundedRect(QRectF(-3.5, -2, 7, 12), 3.5, 3.5)
        painter.restore()
    elif name == "settings":
        points = tuple(
            (12 + radius * cos(angle), 12 + radius * sin(angle))
            for index in range(24)
            for radius, angle in [(8.5 if index % 4 in (1, 2) else 6.8, index * pi / 12)]
        )
        _path(painter, points, closed=True)
        painter.drawEllipse(QRectF(9, 9, 6, 6))
    elif name == "about":
        painter.drawEllipse(QRectF(3, 3, 18, 18))
        _line(painter, 12, 11, 12, 17)
        painter.drawPoint(QPointF(12, 7))
    elif name == "more":
        for x in (5, 12, 19):
            painter.drawEllipse(QRectF(x - 1, 11, 2, 2))
    elif name in {"chevron-down", "chevron-up"}:
        y1, y2 = (9, 15) if name == "chevron-down" else (15, 9)
        _path(painter, ((6, y1), (12, y2), (18, y1)))
    elif name in {"chevron-left", "chevron-right"}:
        x1, x2 = (15, 9) if name == "chevron-left" else (9, 15)
        _path(painter, ((x1, 6), (x2, 12), (x1, 18)))
    elif name == "sun":
        painter.drawEllipse(QRectF(8, 8, 8, 8))
        for index in range(8):
            angle = index * pi / 4
            _line(painter, 12 + 7 * cos(angle), 12 + 7 * sin(angle), 12 + 10 * cos(angle), 12 + 10 * sin(angle))
    elif name == "moon":
        path = QPainterPath(QPointF(19.5, 14.5))
        path.cubicTo(11.5, 17, 7, 10, 10.5, 3.5)
        path.cubicTo(1.5, 5.5, 1.5, 18.5, 10.5, 20.5)
        path.cubicTo(15, 21.5, 18.5, 18.5, 19.5, 14.5)
        painter.drawPath(path)
    elif name == "download":
        _line(painter, 12, 3, 12, 15)
        _path(painter, ((7, 10), (12, 15), (17, 10)))
        _path(painter, ((4, 16), (4, 21), (20, 21), (20, 16)))
    elif name == "close":
        _line(painter, 6, 6, 18, 18)
        _line(painter, 18, 6, 6, 18)
    elif name == "check":
        _path(painter, ((5, 12), (10, 17), (19, 7)))


@lru_cache(maxsize=256)
def _cached_icon(name: str, color: str, size: int) -> QIcon:
    icon = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
        tint = QColor(color)
        if mode == QIcon.Mode.Disabled:
            tint.setAlpha(100)
        for scale in (1, 2):
            pixmap = QPixmap(size * scale, size * scale)
            pixmap.fill(Qt.GlobalColor.transparent)
            pixmap.setDevicePixelRatio(scale)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(size / 24, size / 24)
            painter.setPen(QPen(tint, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            _draw(painter, name)
            painter.end()
            icon.addPixmap(pixmap, mode, QIcon.State.Off)
            icon.addPixmap(pixmap, mode, QIcon.State.On)
            if mode == QIcon.Mode.Normal:
                # Avoid platform hover/selection recolouring in monochrome menus.
                for active_mode in (QIcon.Mode.Active, QIcon.Mode.Selected):
                    icon.addPixmap(pixmap, active_mode, QIcon.State.Off)
                    icon.addPixmap(pixmap, active_mode, QIcon.State.On)
    return icon


def make_icon(name: str, color: str, size: int = 18) -> QIcon:
    """Return a line icon tinted for the current theme, with a dim disabled state."""
    name = _ALIASES.get(name, name)
    if name not in _NAMES:
        raise ValueError(f"Unknown line icon: {name}")
    if size <= 0:
        raise ValueError("Icon size must be positive")
    tint = QColor(color)
    if not tint.isValid():
        raise ValueError(f"Invalid icon colour: {color}")
    return QIcon(_cached_icon(name, tint.name(QColor.NameFormat.HexArgb), size))


class IconComboBox(QComboBox):
    """A native combo box with the same line chevron as the other controls.

    Qt removes its native arrow when the dropdown receives a stylesheet box.
    Only the arrow is painted here; item models, popups, keyboard controls and
    selection signals retain the standard QComboBox implementation.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("lineArrow", True)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxArrow, self,
        )
        rect = QRect(arrow.center().x() - 7, arrow.center().y() - 7, 14, 14)
        color = self.palette().color(QPalette.ColorRole.Text).name()
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        painter = QPainter(self)
        make_icon("chevron-down", color, 14).paint(painter, rect, Qt.AlignmentFlag.AlignCenter, mode)
        painter.end()


class IconSpinBox(QSpinBox):
    """Keep native numeric editing and stepping; paint matching line arrows."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("lineArrow", True)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        color = self.palette().color(QPalette.ColorRole.Text).name()
        painter = QPainter(self)
        for control, icon_name, enabled_flag in (
            (QStyle.SubControl.SC_SpinBoxUp, "chevron-up", QAbstractSpinBox.StepEnabledFlag.StepUpEnabled),
            (QStyle.SubControl.SC_SpinBoxDown, "chevron-down", QAbstractSpinBox.StepEnabledFlag.StepDownEnabled),
        ):
            button = self.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, self)
            rect = QRect(button.center().x() - 6, button.center().y() - 6, 12, 12)
            enabled = self.isEnabled() and bool(option.stepEnabled & enabled_flag)
            mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
            make_icon(icon_name, color, 12).paint(painter, rect, Qt.AlignmentFlag.AlignCenter, mode)
        painter.end()
