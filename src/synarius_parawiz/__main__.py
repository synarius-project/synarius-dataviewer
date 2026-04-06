"""Entry point for Synarius ParaWiz."""

from __future__ import annotations

import argparse
import sys

from synarius_parawiz._version import __version__


def main() -> int:
    parser = argparse.ArgumentParser(description="Synarius ParaWiz")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return 0

    # Windows: AppUserModelID vor Qt, sonst Taskbar-Gruppe/Icon oft bei python.exe.
    if sys.platform.startswith("win"):
        try:
            import ctypes

            from synarius_parawiz.app.windows_app_id import PARAWIZ_APP_USER_MODEL_ID

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
                PARAWIZ_APP_USER_MODEL_ID
            )
        except Exception:
            pass

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from synarius_parawiz.app.icon_utils import parawiz_app_icon, windows_apply_native_taskbar_icon
    from synarius_parawiz.app.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Synarius ParaWiz")
    app.setApplicationVersion(__version__)
    # Align with AppUserModelID (Windows taskbar / jump lists).
    try:
        app.setDesktopFileName("synarius.parawiz")
    except Exception:
        pass
    _icon = parawiz_app_icon()
    app.setWindowIcon(_icon)
    w = MainWindow()
    w.show()
    # Win32: Titelleiste nutzt QIcon; die Taskbar holt oft HICON per WM_SETICON (sonst python.exe).
    if sys.platform.startswith("win"):
        QTimer.singleShot(0, lambda win=w: windows_apply_native_taskbar_icon(win))
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
