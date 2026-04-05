"""Resolve Synarius application icon (Dataviewer / Studio assets) for ParaWiz windows and QApplication."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap


def parawiz_icon_png_path() -> Path | None:
    """Return path to ``synarius64.png`` if found (editable install, site-packages, or monorepo checkout)."""
    candidates: list[Path] = []

    def add(p: Path) -> None:
        if p not in candidates:
            candidates.append(p)

    try:
        import synarius_dataviewer as sdv

        add(Path(sdv.__file__).resolve().parent / "app" / "icons" / "synarius64.png")
    except Exception:
        pass

    try:
        import synarius_parawiz as sp

        pkg = Path(sp.__file__).resolve().parent
        add(pkg.parent / "synarius_dataviewer" / "app" / "icons" / "synarius64.png")
        # Optional copy next to ParaWiz (e.g. wheel without dataviewer icons)
        add(pkg / "icons" / "synarius64.png")
    except Exception:
        pass

    here = Path(__file__).resolve()
    # icon_utils.py -> …/synarius_parawiz/app/ -> parents[2] is ``src`` or ``site-packages``.
    add(here.parents[2] / "synarius_dataviewer" / "app" / "icons" / "synarius64.png")

    for anc in here.parents:
        add(anc / "synarius-studio" / "src" / "synarius_studio" / "icons" / "synarius64.png")
        add(anc / "synarius_studio" / "icons" / "synarius64.png")

    for p in candidates:
        if p.is_file():
            return p
    return None


def _fallback_parawiz_icon_pixmap(edge: int = 64) -> QPixmap:
    """Placeholder when ``synarius64.png`` is not on disk (e.g. asset not shipped in checkout)."""
    e = max(16, int(edge))
    pm = QPixmap(e, e)
    pm.fill(QColor("#586cd4"))
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#ffffff")))
        f = QFont()
        f.setPixelSize(max(10, int(e * 0.52)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), int(Qt.AlignmentFlag.AlignCenter), "S")
    finally:
        p.end()
    return pm


def parawiz_app_icon() -> QIcon:
    """Icon for title bar and Windows taskbar (PNG if present, else built-in fallback)."""
    path = parawiz_icon_png_path()
    if path is not None:
        return QIcon(str(path))
    ico = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ico.addPixmap(_fallback_parawiz_icon_pixmap(s))
    return ico
