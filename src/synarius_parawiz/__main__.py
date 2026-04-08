"""Entry point for Synarius ParaWiz."""

from __future__ import annotations

import argparse
import os
import sys

from synarius_apps_diagnostics import (
    configure_file_logging,
    install_qt_message_handler,
    log_session_start,
    main_log_path,
)
from synarius_parawiz._version import __version__


def main() -> int:
    parser = argparse.ArgumentParser(description="Synarius ParaWiz")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return 0

    configure_file_logging(
        user_log_appname="SynariusParawiz",
        log_filename="synarius-parawiz.log",
        uncaught_logger_name="synarius_parawiz.uncaught",
        root_child_logger="synarius_parawiz",
        debug_env_keys=("SYNARIUS_PARAWIZ_LOG_DEBUG",),
    )
    log_session_start(logger_name="synarius_parawiz.bootstrap", app_name="Synarius ParaWiz", version=__version__)
    _log_path = main_log_path()
    if _log_path is not None:
        print(
            f"Synarius ParaWiz {__version__} | log file: {_log_path.resolve()}",
            file=sys.stderr,
            flush=True,
        )
    if os.environ.get("SYNARIUS_PARAWIZ_PROFILE", "").strip().lower() in ("1", "true", "yes", "on"):
        print(
            "ParaWiz: SYNARIUS_PARAWIZ_PROFILE aktiv — Laufzeitzeilen (collect/populate/refresh + Kopieren) auf stderr.",
            file=sys.stderr,
            flush=True,
        )
        print(
            "ParaWiz: Cross-Dataset-Farben optional SYNARIUS_PARAWIZ_CROSS_STYLE_MAX_ROWS "
            "(Standard 12000; Namensfilter reduziert Aufwand).",
            file=sys.stderr,
            flush=True,
        )
    elif os.environ.get("SYNARIUS_PARAWIZ_PROFILE_COPY", "").strip().lower() in ("1", "true", "yes", "on"):
        print(
            "ParaWiz: SYNARIUS_PARAWIZ_PROFILE_COPY aktiv — nur Kopier-Pfad (parawiz profile copy: …) auf stderr.",
            file=sys.stderr,
            flush=True,
        )

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
    install_qt_message_handler()
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
