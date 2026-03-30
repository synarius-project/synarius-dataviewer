"""Entry: graphical Dataviewer (PySide6) or ``--version``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from synarius_dataviewer._version import __version__


def main() -> int:
    parser = argparse.ArgumentParser(description="Synarius Dataviewer")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return 0

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from synarius_dataviewer.app.main_window import MainWindow

    if sys.platform.startswith("win"):
        # Ensure Windows taskbar uses Synarius identity/icon instead of python.exe.
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
                "synarius.dataviewer"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("Synarius Dataviewer")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(
        QIcon(str(Path(__file__).resolve().parent / "app" / "icons" / "synarius64.png"))
    )
    w = MainWindow()
    w.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
