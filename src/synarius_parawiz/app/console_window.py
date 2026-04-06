from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QMainWindow, QPushButton, QPlainTextEdit, QVBoxLayout, QWidget
from synarius_core.controller import CommandError, MinimalController

from synarius_dataviewer.app import theme


class ConsoleWindow(QMainWindow):
    """Standalone REPL view backed by the shared MinimalController instance."""

    def __init__(
        self,
        controller: MinimalController,
        *,
        on_command_executed: Callable[[], None],
        app_icon: QIcon | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._on_command_executed = on_command_executed
        self.setWindowTitle("Synarius ParaWiz — CLI")
        self.resize(960, 520)
        if app_icon is not None and not app_icon.isNull():
            self.setWindowIcon(app_icon)

        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._log.setFont(mono)
        self._log.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme.CONSOLE_CHROME_BACKGROUND}; color: {theme.CONSOLE_TAB_TEXT}; border: none; }}"
        )
        layout.addWidget(self._log, stretch=1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._cmd = QLineEdit(self)
        self._cmd.setPlaceholderText("Enter command, e.g. cd @main/parameters/data_sets")
        self._cmd.returnPressed.connect(self._execute_current)
        row.addWidget(self._cmd, stretch=1)
        self._run = QPushButton("Run", self)
        self._run.clicked.connect(self._execute_current)
        row.addWidget(self._run, stretch=0)
        layout.addLayout(row)

        self._append("INFO", "synarius_parawiz.console", "console started")

    def append_parawiz_ccp(self, cmd: str, result: str | None = None, error: str | None = None) -> None:
        """Protokolliert CCP-Zeilen aus ParaWiz (z. B. nach Apply im Kennfeld), analog zum REPL."""
        self._append("INFO", "synarius_parawiz.ccp", f"command [parawiz]: {cmd}")
        if error:
            self._append("ERROR", "synarius_parawiz.ccp", error)
        elif result is not None:
            if len(cmd) > 240:
                self._append("INFO", "synarius_parawiz.ccp", f"command [parawiz] ok -> {result}")
            else:
                self._append("INFO", "synarius_parawiz.ccp", f"command [parawiz] ok: {cmd} -> {result}")

    def _now(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _append(self, level: str, logger: str, message: str) -> None:
        self._log.appendPlainText(f"{self._now()} {level:<5} {logger} | {message}")
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())

    def _execute_current(self) -> None:
        cmd = self._cmd.text().strip()
        if not cmd:
            return
        self._cmd.clear()
        self._append("INFO", "synarius_parawiz.console", f"command [repl]: {cmd}")
        try:
            out = self._controller.execute(cmd)
            suffix = f" -> {out}" if out else ""
            self._append("INFO", "synarius_parawiz.console", f"command [repl] ok: {cmd}{suffix}")
            self._on_command_executed()
        except CommandError as exc:
            self._append("ERROR", "synarius_parawiz.console", str(exc))
        except Exception as exc:
            self._append("ERROR", "synarius_parawiz.console", str(exc))

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
