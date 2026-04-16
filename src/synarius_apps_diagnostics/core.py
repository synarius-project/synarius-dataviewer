"""Rotating file logs, sys/threading excepthook, faulthandler (default on; opt-out via env), Qt message handler."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import Sequence

_main_log_path: Path | None = None
_file_configured = False
_prev_excepthook = None
_threading_hook_installed = False
_prev_threading_excepthook = None
_qt_handler_installed = False


def main_log_path() -> Path | None:
    return _main_log_path


def log_directory_for_app(*, appname: str, appauthor: str = "Synarius") -> Path:
    """Per-user log directory (creates if missing)."""
    try:
        from platformdirs import user_log_dir

        base = user_log_dir(appname=appname, appauthor=appauthor)
    except ImportError:
        if sys.platform.startswith("win"):
            local = os.environ.get("LOCALAPPDATA", "")
            safe = appname.replace(" ", "")
            base = (
                str(Path(local) / "Synarius" / safe / "Logs")
                if local
                else str(Path.home() / f".{safe.lower()}" / "logs")
            )
        elif sys.platform == "darwin":
            base = str(Path.home() / "Library" / "Logs" / appname.replace(" ", ""))
        else:
            base = str(Path.home() / ".local" / "share" / appname.replace(" ", "") / "logs")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _debug_from_env(env_keys: Sequence[str]) -> bool:
    for k in env_keys:
        if os.environ.get(k, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    if os.environ.get("SYNARIUS_LOG_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return False


def _fault_handler_disabled_by_env() -> bool:
    """When ``SYNARIUS_FAULT_HANDLER`` is explicitly ``0``/``false``/``no``/``off``, skip ``faulthandler``."""
    v = os.environ.get("SYNARIUS_FAULT_HANDLER", "").strip().lower()
    if not v:
        return False
    return v in {"0", "false", "no", "off"}


def configure_file_logging(
    *,
    user_log_appname: str,
    log_filename: str,
    uncaught_logger_name: str,
    root_child_logger: str | None = None,
    debug_env_keys: Sequence[str] = (),
    appauthor: str = "Synarius",
) -> Path:
    """Rotating file on root logger, warnings to logging, excepthooks. Safe to call once per process."""
    global _file_configured, _main_log_path, _prev_excepthook, _threading_hook_installed

    if _file_configured:
        return _main_log_path.parent if _main_log_path is not None else log_directory_for_app(
            appname=user_log_appname, appauthor=appauthor
        )

    log_dir = log_directory_for_app(appname=user_log_appname, appauthor=appauthor)
    log_path = log_dir / log_filename
    _main_log_path = log_path

    keys = tuple(debug_env_keys)
    level = logging.DEBUG if _debug_from_env(keys) else logging.INFO

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    target = str(log_path.resolve())
    has_same = any(
        isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "baseFilename", None) == target
        for h in root.handlers
    )
    if not has_same:
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=9,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    root.setLevel(level)
    if root_child_logger:
        logging.getLogger(root_child_logger).setLevel(level)
    for noisy in ("urllib3", "matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.captureWarnings(True)
    _install_excepthook(uncaught_logger_name)
    _install_threading_excepthook(uncaught_logger_name)

    if not _fault_handler_disabled_by_env():
        try:
            import faulthandler

            with log_path.open("a", encoding="utf-8") as fh:
                faulthandler.enable(file=fh, all_threads=True)
        except Exception:
            logging.getLogger("synarius_apps_diagnostics").warning("faulthandler.enable failed", exc_info=True)

    logging.getLogger("synarius_apps_diagnostics").info("Logging initialized; log file: %s", log_path)
    _file_configured = True
    return log_dir


def _install_excepthook(uncaught_logger_name: str) -> None:
    global _prev_excepthook
    if _prev_excepthook is not None:
        return
    _prev_excepthook = sys.excepthook
    log = logging.getLogger(uncaught_logger_name)

    def _hook(exc_type, exc, tb) -> None:
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        if _prev_excepthook is not None:
            _prev_excepthook(exc_type, exc, tb)

    sys.excepthook = _hook


def _install_threading_excepthook(uncaught_logger_name: str) -> None:
    global _threading_hook_installed, _prev_threading_excepthook
    if _threading_hook_installed:
        return
    prev = getattr(threading, "excepthook", None)
    if prev is None:
        return
    _prev_threading_excepthook = prev
    log = logging.getLogger(uncaught_logger_name)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:  # type: ignore[name-defined]
        t = getattr(getattr(args, "thread", None), "name", None)
        exc_type = getattr(args, "exc_type", None)
        exc_value = getattr(args, "exc_value", None)
        exc_tb = getattr(args, "exc_traceback", None)
        log.critical(
            "Uncaught thread exception (thread=%s): %s",
            t,
            exc_value,
            exc_info=(exc_type, exc_value, exc_tb) if exc_type is not None else None,
        )
        if _prev_threading_excepthook is not None:
            _prev_threading_excepthook(args)

    threading.excepthook = _thread_hook  # type: ignore[attr-defined, assignment]
    _threading_hook_installed = True


def log_session_start(
    *,
    logger_name: str,
    app_name: str,
    version: str,
) -> None:
    """One INFO line after file logging is configured."""
    p = _main_log_path
    logging.getLogger(logger_name).info(
        "session_start app=%s version=%s pid=%s python=%s platform=%s log_file=%s",
        app_name,
        version,
        os.getpid(),
        sys.version.split()[0],
        sys.platform,
        str(p.resolve()) if p is not None else "",
    )


def install_qt_message_handler() -> None:
    """Log Qt qDebug/qWarning etc. via Python logging (call after QApplication exists)."""
    global _qt_handler_installed
    if _qt_handler_installed:
        return
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    _map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
        QtMsgType.QtInfoMsg: logging.INFO,
    }

    def _qt_handler(mode, context, message: str) -> None:
        lvl = _map.get(mode, logging.WARNING)
        extra = ""
        if context.file:
            extra = f" ({context.file}:{context.line})"
        logging.getLogger("qt").log(lvl, "%s%s", message, extra)

    qInstallMessageHandler(_qt_handler)
    _qt_handler_installed = True
