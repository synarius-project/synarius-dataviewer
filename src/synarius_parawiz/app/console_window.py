from __future__ import annotations

from typing import Callable

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget
from synarius_core.controller import CommandError, MinimalController

from synarius_dataviewer.app import theme
from synariustools.tools.terminal_console import TerminalConsoleWidget

# Sehr große CCP-Ausgaben (z. B. ``ls`` im Wurzelverzeichnis) als eine Zeile → Speicher/Widgets belasten.
_MAX_REPL_OUTPUT_CHARS = 120_000
# Protokoll: riesige ``select``/``select -p``-Zeilen (Zehntausend Refs) → Terminal-Widget/Qt
_MAX_PROTOCOL_CMD_CHARS = 24_000
DEFAULT_OUTPUT_COLOR = "#ADD8E6"
DEFAULT_PROMPT_COLOR = "#90EE90"
DEFAULT_ERROR_COLOR = "#FF6666"
DEFAULT_INPUT_COLOR = "#FFFFFF"


class ConsoleWindow(QMainWindow):
    """Standalone REPL view backed by shared execute callback."""

    def __init__(
        self,
        *,
        controller: MinimalController,
        on_execute_line: Callable[[str, str], str | None],
        prompt_provider: Callable[[], str],
        on_command_executed: Callable[[str], None],
        app_icon: QIcon | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._on_execute_line = on_execute_line
        self._prompt_provider = prompt_provider
        self._on_command_executed = on_command_executed
        self._default_output_color = DEFAULT_OUTPUT_COLOR
        self.setWindowTitle("Synarius ParaWiz — CLI")
        self.resize(960, 520)
        if app_icon is not None and not app_icon.isNull():
            self.setWindowIcon(app_icon)

        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._console = TerminalConsoleWidget(
            self._on_submit,
            self._history_prev,
            self._history_next,
            self,
            input_color=DEFAULT_INPUT_COLOR,
            output_color=self._default_output_color,
        )
        self._console.setStyleSheet(
            f"background-color: {theme.CONSOLE_CHROME_BACKGROUND}; "
            f"color: {theme.CONSOLE_TAB_TEXT}; "
            "font-family: Consolas, Courier New, monospace; "
            "border: none;"
        )
        layout.addWidget(self._console, 1)

        self._history: list[str] = []
        self._history_idx = 0
        self._console.append_output("synarius-core minimal CLI", self._default_output_color)
        self._console.append_output("Type 'help' for commands, 'exit' to close.", self._default_output_color)
        self._show_prompt()

    def _show_prompt(self) -> None:
        self._console.show_prompt(f"{self._prompt_provider()}> ", DEFAULT_PROMPT_COLOR)

    def _insert_log(self, text: str, color: str) -> None:
        self._console.insert_log_before_current_prompt(text, color)

    def append_protocol_command(self, cmd: str) -> None:
        s = cmd.strip()
        if len(s) > _MAX_PROTOCOL_CMD_CHARS:
            s = (
                s[:_MAX_PROTOCOL_CMD_CHARS]
                + f"\n... [truncated, command was {len(cmd.strip())} chars]"
            )
        self._insert_log(f"{self._prompt_provider()}> {s}", DEFAULT_PROMPT_COLOR)

    def append_protocol_result(self, result: str) -> None:
        rs = str(result)
        if len(rs) > _MAX_REPL_OUTPUT_CHARS:
            rs = rs[:_MAX_REPL_OUTPUT_CHARS] + f"\n... [truncated, result was {len(str(result))} chars]"
        self._insert_log(rs, self._default_output_color)

    def append_protocol_error(self, error: str) -> None:
        self._insert_log(f"error: {error}", DEFAULT_ERROR_COLOR)

    def append_repl_result(self, result: str) -> None:
        rs = str(result)
        if len(rs) > _MAX_REPL_OUTPUT_CHARS:
            rs = rs[:_MAX_REPL_OUTPUT_CHARS] + f"\n... [truncated, result was {len(str(result))} chars]"
        self._console.append_output(rs, self._default_output_color)

    def append_repl_error(self, error: str) -> None:
        self._console.append_output(f"error: {error}", DEFAULT_ERROR_COLOR)

    def append_parawiz_ccp(self, cmd: str, result: str | None = None, error: str | None = None) -> None:
        """Backward-compatible shim for existing callers."""
        self.append_protocol_command(cmd)
        if error:
            self.append_protocol_error(error)
        elif result is not None:
            self.append_protocol_result(str(result))

    def _history_prev(self) -> None:
        if not self._history:
            return
        self._history_idx = max(0, self._history_idx - 1)
        self._console.replace_current_input(self._history[self._history_idx])

    def _history_next(self) -> None:
        if not self._history:
            return
        self._history_idx = min(len(self._history), self._history_idx + 1)
        if self._history_idx >= len(self._history):
            self._console.replace_current_input("")
            return
        self._console.replace_current_input(self._history[self._history_idx])

    def _on_submit(self, line: str) -> None:
        cmd = line.strip()
        if not cmd:
            self._show_prompt()
            return

        self._history.append(line)
        self._history_idx = len(self._history)

        if cmd in {"exit", "quit"}:
            self.close()
            return
        if cmd == "help":
            self._console.append_output("Built-in commands:", self._default_output_color)
            self._console.append_output("  help                    Show this help", self._default_output_color)
            self._console.append_output("  exit | quit             Close CLI window", self._default_output_color)
            self._console.append_output(
                "  load <file.syn>         Load command-stack script",
                self._default_output_color,
            )
            self._console.append_output("", self._default_output_color)
            self._console.append_output("Protocol commands:", self._default_output_color)
            self._console.append_output(
                "  ls, lsattr [-l], cd <path>, new ..., select ... (-p append, -m remove), "
                "set ..., get ..., del ... | del @selected",
                self._default_output_color,
            )
            self._show_prompt()
            return

        try:
            self._on_execute_line(cmd, "repl")
            self._on_command_executed(cmd)
        except (CommandError, Exception):
            # Errors are rendered by the shared execute/logging path.
            pass
        self._show_prompt()

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
