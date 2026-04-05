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

    # Windows: set AppUserModelID before any Qt import so the taskbar can group this process
    # under synarius.parawiz instead of generic python.exe.
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
                "synarius.parawiz"
            )
        except Exception:
            pass

    from PySide6.QtWidgets import QApplication

    from synarius_parawiz.app.icon_utils import parawiz_app_icon
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
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
