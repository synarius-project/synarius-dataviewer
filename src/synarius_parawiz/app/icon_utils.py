"""Resolve Synarius application icon (Dataviewer / Studio assets) for ParaWiz windows and QApplication."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

# Einmal erzeugen: mehrere Größen helfen Windows-Taskbar und HiDPI-Titelleisten.
_PARAWIZ_APP_ICON_CACHE: QIcon | None = None


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


def _build_parawiz_app_icon() -> QIcon:
    """Mehrere eingetragene Pixmap-Größen (besser als einzelnes PNG für Taskbar/Scaling)."""
    ico = QIcon()
    path = parawiz_icon_png_path()
    if path is not None:
        pm0 = QPixmap(str(path))
        if not pm0.isNull():
            for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
                pm = pm0.scaled(
                    s,
                    s,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                ico.addPixmap(pm)
            return ico
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        ico.addPixmap(_fallback_parawiz_icon_pixmap(s))
    return ico


def parawiz_app_icon() -> QIcon:
    """Synarius-Icon für QApplication, Hauptfenster, Dialoge und Taskbar (Windows: mit AppUserModelID)."""
    global _PARAWIZ_APP_ICON_CACHE
    if _PARAWIZ_APP_ICON_CACHE is None:
        _PARAWIZ_APP_ICON_CACHE = _build_parawiz_app_icon()
    return _PARAWIZ_APP_ICON_CACHE


def _write_ico_embedded_png(png_path: Path, ico_path: str) -> bool:
    """ICO mit eingebettetem PNG (Windows Vista+), falls Qt kein ICO schreiben kann."""
    try:
        png = png_path.read_bytes()
    except OSError:
        return False
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    w = int.from_bytes(png[16:20], "big")
    h = int.from_bytes(png[20:24], "big")
    if w <= 0 or h <= 0:
        return False
    wb = 0 if w >= 256 else w
    hb = 0 if h >= 256 else h
    offset = 6 + 16
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", wb, hb, 0, 0, 1, 32, len(png), offset)
    try:
        with open(ico_path, "wb") as f:
            f.write(header)
            f.write(entry)
            f.write(png)
    except OSError:
        return False
    return True


def windows_apply_native_taskbar_icon(widget: object) -> bool:
    """Windows: ``WM_SETICON`` mit ``HICON`` aus temporärer ICO — Taskbar zeigt sonst oft das EXE-Icon (python).

    Qt setzt zwar ``setWindowIcon``, die Shell verwendet für die Taskbar aber häufig die nativen Icons.
    """
    if not sys.platform.startswith("win"):
        return False
    win_id = getattr(widget, "winId", None)
    if not callable(win_id):
        return False
    try:
        hwnd = int(win_id())
    except Exception:
        return False
    if hwnd <= 0:
        return False

    png_path = parawiz_icon_png_path()
    if png_path is not None:
        pm = QPixmap(str(png_path))
    else:
        pm = _fallback_parawiz_icon_pixmap(256)
    if pm.isNull():
        return False

    fd, ico_path = tempfile.mkstemp(suffix=".ico", prefix="synarius_parawiz_")
    os.close(fd)
    try:
        wrote = pm.save(ico_path, "ICO")
        if not wrote and png_path is not None:
            wrote = _write_ico_embedded_png(png_path, ico_path)
        if not wrote:
            fd2, png_tmp = tempfile.mkstemp(suffix=".png", prefix="synarius_parawiz_")
            os.close(fd2)
            try:
                if pm.save(png_tmp, "PNG"):
                    wrote = _write_ico_embedded_png(Path(png_tmp), ico_path)
            finally:
                try:
                    os.unlink(png_tmp)
                except OSError:
                    pass
        if not wrote:
            return False
        import ctypes

        user32 = ctypes.windll.user32
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        hico = user32.LoadImageW(
            None,
            ctypes.c_wchar_p(ico_path),
            IMAGE_ICON,
            0,
            0,
            LR_LOADFROMFILE,
        )
        if not hico:
            return False
        WM_SETICON = 0x0080
        user32.SendMessageW(hwnd, WM_SETICON, 0, hico)
        user32.SendMessageW(hwnd, WM_SETICON, 1, hico)
        return True
    finally:
        try:
            os.unlink(ico_path)
        except OSError:
            pass
