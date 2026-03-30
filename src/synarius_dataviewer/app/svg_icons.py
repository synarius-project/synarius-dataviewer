"""Tint monochrome SVG icons for dark toolbars."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_HEX_DARK_1 = re.compile(r"#232629", re.IGNORECASE)
_HEX_DARK_2 = re.compile(r"#1c1c1c", re.IGNORECASE)
_HEX_DARK_3 = re.compile(r"#000000", re.IGNORECASE)


def _tint_svg_markup(svg_text: str, foreground: QColor) -> str:
    hx = foreground.name(QColor.NameFormat.HexRgb)
    s = _HEX_DARK_1.sub(hx, svg_text)
    s = _HEX_DARK_2.sub(hx, s)
    s = _HEX_DARK_3.sub(hx, s)
    return s


def icon_from_tinted_svg_file(
    svg_path: Path,
    foreground: QColor,
    *,
    logical_side: int = 20,
) -> QIcon:
    raw = svg_path.read_text(encoding="utf-8")
    tinted = _tint_svg_markup(raw, foreground)
    renderer = QSvgRenderer(QByteArray(tinted.encode("utf-8")))
    if not renderer.isValid():
        return QIcon(str(svg_path))

    app = QGuiApplication.instance()
    dpr = 1.0
    if app is not None:
        screen = app.primaryScreen()
        if screen is not None:
            dpr = max(1.0, float(screen.devicePixelRatio()))

    px = max(1, int(round(logical_side * dpr)))
    img = QImage(px, px, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(p, QRectF(0.0, 0.0, float(px), float(px)))
    p.end()

    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return QIcon(pm)
