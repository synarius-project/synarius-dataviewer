"""Main window for Synarius ParaWiz."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from synarius_core.controller import MinimalController
from synarius_core.model.data_model import ComplexInstance

from synarius_dataviewer.app import theme
from synarius_parawiz._version import __version__
from synarius_parawiz.app.console_window import ConsoleWindow
from synarius_parawiz.app.icon_utils import parawiz_app_icon
from synarius_parawiz.app.status_progress_widget import StatusMessageProgressBar


class MainWindow(QMainWindow):
    # Status bar: show busy/determinate progress for larger DCM loads
    _DCM_PARSE_PROGRESS_MIN_BYTES = 100 * 1024
    _DCM_IMPORT_PROGRESS_MIN_SPECS = 30
    # Zielanteil der Leiste für Tabellen-Aufbau (nach 3*n); skaliert mit n, sonst kaum sichtbar.
    _DCM_TABLE_BAR_SHARE = 0.18
    _DCM_TABLE_BAR_SLOTS_MIN = 500
    # Wide enough for full German status lines, e.g. "DCM · Import 9999 / 10000"
    _DCM_STATUS_PROGRESS_WIDTH = 340
    _DCM_STATUS_PROGRESS_INNER_HEIGHT = 12

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Synarius Apps - ParaWiz {__version__}")
        self.resize(1100, 720)
        self._app_icon = parawiz_app_icon()
        if not self._app_icon.isNull():
            self.setWindowIcon(self._app_icon)

        self._controller = MinimalController()
        self._console_window: ConsoleWindow | None = None
        # Modeless plot dialogs (avoid GC + track for optional future use)
        self._open_param_viewers: list[QDialog] = []
        self._dcm_import_status_frame: StatusMessageProgressBar | None = None
        self._dcm_import_status_frame_in_bar = False
        self._dcm_import_file_bytes = 0
        self._dcm_import_write_total = 0

        self._table = QTableWidget(self)
        self._table.setObjectName("ParameterTable")
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Name", "Type", "Value / Shape"])
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._table.setColumnWidth(0, 340)
        self._table.setColumnWidth(1, 120)
        self.setCentralWidget(self._table)
        self._table.setStyleSheet(self._table_stylesheet())
        self._table.cellDoubleClicked.connect(self._on_parameter_table_double_clicked)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self.statusBar().showMessage("Ready")
        self._refresh_table()
        self.statusBar().showMessage(
            'Doppelklick auf „Value / Shape“ öffnet ein Plot-Fenster; mehrere Fenster können gleichzeitig offen sein.',
            10000,
        )
        # Preload HoloViews + calmapwidget after the first event-loop tick so startup stays responsive
        # but the first double-click on a parameter is not blocked by a multi-second HoloViews/calmap import.
        QTimer.singleShot(0, self._warm_calibration_plot_stack)

    def _warm_calibration_plot_stack(self) -> None:
        try:
            import synariustools.tools.calmapwidget  # noqa: F401
        except Exception:
            pass

    def _register_modeless_param_viewer(self, dlg: QDialog) -> None:
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.setModal(False)

        def _on_destroyed(_: object | None = None, *, ref: QDialog = dlg) -> None:
            try:
                self._open_param_viewers.remove(ref)
            except ValueError:
                pass

        dlg.destroyed.connect(_on_destroyed)
        self._open_param_viewers.append(dlg)

    @staticmethod
    def _dcm_table_bar_slots(n_specs: int) -> int:
        """Progress units for post-import table refresh; ~``_DCM_TABLE_BAR_SHARE`` of the full bar."""
        core = 3 * max(0, int(n_specs))
        if core <= 0:
            return MainWindow._DCM_TABLE_BAR_SLOTS_MIN
        s = float(MainWindow._DCM_TABLE_BAR_SHARE)
        s = min(0.45, max(0.05, s))
        slots = int(round((s / (1.0 - s)) * float(core)))
        return max(MainWindow._DCM_TABLE_BAR_SLOTS_MIN, slots)

    def _dcm_import_accent_color(self) -> str:
        return theme.STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND

    def _dcm_import_ensure_status_frame(self) -> StatusMessageProgressBar:
        if self._dcm_import_status_frame is None:
            self._dcm_import_status_frame = StatusMessageProgressBar(
                accent_color=self._dcm_import_accent_color(),
                bar_width=self._DCM_STATUS_PROGRESS_WIDTH,
                bar_height=self._DCM_STATUS_PROGRESS_INNER_HEIGHT,
                parent=self,
            )
        else:
            self._dcm_import_status_frame.set_accent_color(self._dcm_import_accent_color())
        sb = self.statusBar()
        if not self._dcm_import_status_frame_in_bar:
            # stretch 0 = narrow fixed width, left-aligned in the status bar
            sb.addWidget(self._dcm_import_status_frame, 0)
            self._dcm_import_status_frame_in_bar = True
        sb.clearMessage()
        self._dcm_import_status_frame.show()
        return self._dcm_import_status_frame

    def _dcm_import_remove_progress_bar(self) -> None:
        fr = self._dcm_import_status_frame
        if fr is None:
            return
        if self._dcm_import_status_frame_in_bar:
            self.statusBar().removeWidget(fr)
            self._dcm_import_status_frame_in_bar = False
        fr.hide()

    def _dcm_import_phase(self, phase: str, n: int) -> None:
        show_parse_busy = self._dcm_import_file_bytes >= self._DCM_PARSE_PROGRESS_MIN_BYTES
        if phase == "reading":
            if show_parse_busy:
                w = self._dcm_import_ensure_status_frame()
                w.set_range(0, 0)
                w.set_message("DCM-Datei wird gelesen …")
            else:
                self.statusBar().showMessage("DCM-Datei wird gelesen …")
        elif phase == "parsing":
            if show_parse_busy:
                w = self._dcm_import_ensure_status_frame()
                w.set_range(0, 0)
                w.set_message("DCM wird geparst …")
            else:
                self.statusBar().showMessage("DCM wird geparst …")
        elif phase == "write":
            self._dcm_import_write_total = n
            if n >= self._DCM_IMPORT_PROGRESS_MIN_SPECS:
                w = self._dcm_import_ensure_status_frame()
                # Eine Skala für Modell + DuckDB + Virtuals + Tabellen-GUI (kein zweiter 0→100%-Lauf).
                umax_full = max(1, 3 * n + self._dcm_table_bar_slots(n))
                w.set_range(0, umax_full)
                w.set_value(0)
                w.set_message(f"DCM · Modell 0 / {n}")
            else:
                if self._dcm_import_status_frame_in_bar:
                    self._dcm_import_remove_progress_bar()
                self.statusBar().showMessage(f"DCM · Import ({n} Parameter) …")
        elif phase == "virtuals":
            # Fortschritt kommt aus progress_hook (einheitliche Skala); nur UI pumpen
            pass
        elif phase == "complete":
            # Fortschritt bleibt bei 3*n stehen bis _refresh_table auf derselben Skala weiterzählt.
            pass
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    @staticmethod
    def _dcm_cooperative_ui() -> None:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def _dcm_import_progress(self, done: int, umax: int) -> None:
        """``umax`` aus dem Core ist ``3 * n``; die Statusleiste nutzt ``3*n + Tabelle`` als Endwert."""
        t = self._dcm_import_write_total
        if t < self._DCM_IMPORT_PROGRESS_MIN_SPECS:
            return
        fr = self._dcm_import_status_frame
        if fr is not None and self._dcm_import_status_frame_in_bar:
            umax_full = max(1, 3 * t + self._dcm_table_bar_slots(t))
            fr.set_range(0, umax_full)
            fr.set_value(min(int(done), umax_full))
            if umax == 3 * t and t > 0:
                if done <= t:
                    sub, phase, subtot = done, "Modell", t
                elif done <= 2 * t:
                    sub, phase, subtot = done - t, "Speichern", t
                else:
                    sub, phase, subtot = done - 2 * t, "Einrichten", t
                pct = int(100 * int(done) / umax_full)
                fr.set_message(f"DCM · {phase} {sub} / {subtot}  ({pct}%)")
            else:
                fr.set_message(f"DCM · {done} / {umax}")
        app = QApplication.instance()
        if app is None:
            return
        step = 1 if umax > 2400 else (3 if umax > 600 else (8 if umax > 120 else 25))
        if done == 1 or done % step == 0 or done == umax:
            app.processEvents()

    def _table_stylesheet(self) -> str:
        return (
            "QTableWidget {"
            f" background-color: {theme.RESOURCES_PANEL_BACKGROUND};"
            f" alternate-background-color: {theme.RESOURCES_PANEL_ALTERNATE_ROW};"
            " color: #1a1a1a;"
            " gridline-color: transparent;"
            " border: none;"
            " font-size: 11px;"
            "}"
            "QTableWidget::item { padding: 0px 2px; }"
            "QTableWidget::item:selected {"
            " background-color: #586cd4;"
            " color: #ffffff;"
            "}"
            "QHeaderView::section {"
            " background-color: #353535;"
            " color: #ffffff;"
            " padding: 2px 4px;"
            " border: none;"
            " font-size: 11px;"
            "}"
            # Dark scrollbars (aligned with synarius_dataviewer.app.theme channel panel)
            "QScrollBar:vertical { background: #2f2f2f; width: 12px; margin: 0; border: none; }"
            "QScrollBar::handle:vertical { background: #5a5a5a; min-height: 20px; border-radius: 4px; }"
            "QScrollBar::handle:vertical:hover { background: #6a6a6a; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; background: none; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #2f2f2f; }"
            "QScrollBar:horizontal { background: #2f2f2f; height: 12px; margin: 0; border: none; }"
            "QScrollBar::handle:horizontal { background: #5a5a5a; min-width: 20px; border-radius: 4px; }"
            "QScrollBar::handle:horizontal:hover { background: #6a6a6a; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; background: none; }"
            "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: #2f2f2f; }"
        )

    def _create_actions(self) -> None:
        self._act_open_script = QAction("Open Parameter Script...", self)
        self._act_open_script.triggered.connect(self._open_script)
        self._act_open_source = QAction("Register DataSet Source...", self)
        self._act_open_source.triggered.connect(self._register_data_set_source)
        self._act_refresh = QAction("Refresh", self)
        self._act_refresh.triggered.connect(self._refresh_table)
        self._act_console = QAction("CLI Console", self)
        self._act_console.triggered.connect(self._open_console)

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(self._act_open_script)
        file_menu.addAction(self._act_open_source)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.close)

        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self._act_refresh)
        view_menu.addAction(self._act_console)

    def _create_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setStyleSheet(theme.studio_toolbar_stylesheet())
        tb.addAction(self._act_open_script)
        tb.addAction(self._act_open_source)
        tb.addAction(self._act_refresh)
        tb.addSeparator()
        tb.addAction(self._act_console)

    def _open_console(self) -> None:
        if self._console_window is None:
            self._console_window = ConsoleWindow(
                self._controller,
                on_command_executed=self._refresh_table,
                app_icon=self._app_icon,
            )
        self._console_window.show_and_raise()

    def _open_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open parameter script",
            "",
            "Synarius scripts (*.syn *.txt *.cli);;All files (*)",
        )
        if not path:
            return
        cli_path = path.replace("\\", "/")
        try:
            self._controller.execute(f'load "{cli_path}"')
            self._refresh_table()
            self.statusBar().showMessage(f"Loaded script: {Path(path).name}", 6000)
        except Exception as exc:
            QMessageBox.critical(self, "Load script failed", str(exc))

    def _register_data_set_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Register DataSet source",
            "",
            "Parameter files (*.dcm *.cdfx *.a2l);;All files (*)",
        )
        if not path:
            return
        fmt = (Path(path).suffix or "").lstrip(".").lower() or "unknown"
        name = self._next_dataset_name(Path(path).stem)
        cli_path = path.replace("\\", "/")
        try:
            self._controller.execute("cd @main/parameters/data_sets")
            ds_ref = (self._controller.execute(f'new DataSet {name} source_path="{cli_path}" source_format={fmt}') or "").strip()
            if fmt == "dcm":
                from synarius_core.parameters.dcm_io import import_dcm_for_dataset

                fpath = Path(path).resolve()
                self._dcm_import_file_bytes = fpath.stat().st_size
                n_imported = 0
                dcm_table_gui = False
                try:
                    n_imported = import_dcm_for_dataset(
                        self._controller,
                        ds_ref,
                        str(fpath),
                        import_phase_hook=self._dcm_import_phase,
                        progress_hook=self._dcm_import_progress,
                        cooperative_hook=self._dcm_cooperative_ui,
                    )
                    dcm_table_gui = (
                        self._dcm_import_write_total >= self._DCM_IMPORT_PROGRESS_MIN_SPECS
                        and self._dcm_import_status_frame_in_bar
                    )
                finally:
                    self._dcm_import_file_bytes = 0
                    if not dcm_table_gui:
                        self._dcm_import_remove_progress_bar()
                        self._dcm_import_write_total = 0
                if dcm_table_gui:
                    try:
                        self._refresh_table(
                            dcm_table_progress=True,
                            dcm_imported_hint=max(1, n_imported),
                        )
                    finally:
                        self._dcm_import_remove_progress_bar()
                        self._dcm_import_write_total = 0
                else:
                    self._refresh_table()
                self.statusBar().showMessage(f"DCM: {n_imported} parameters in DataSet '{name}'", 8000)
            else:
                self._refresh_table()
                self.statusBar().showMessage(f"Registered source for DataSet '{name}'", 6000)
        except Exception as exc:
            QMessageBox.critical(self, "Register DataSet failed", str(exc))

    def _next_dataset_name(self, raw: str) -> str:
        base = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in raw).strip("_")
        if not base:
            base = "DataSet"
        used: set[str] = set()
        root = self._controller.model.parameter_runtime().data_sets_root()
        for child in root.children:
            if isinstance(child, ComplexInstance):
                used.add(child.name)
        if base not in used:
            return base
        idx = 2
        while f"{base}_{idx}" in used:
            idx += 1
        return f"{base}_{idx}"

    def _collect_rows(
        self,
        *,
        on_seen: Callable[[int], None] | None = None,
        seen_every: int = 100,
    ) -> list[tuple[str, str, str, UUID]]:
        rows: list[tuple[str, str, str, UUID]] = []
        model = self._controller.model
        repo = model.parameter_runtime().repo
        seen = 0
        for node in model.iter_objects():
            if not isinstance(node, ComplexInstance):
                continue
            if model.is_in_trash_subtree(node):
                continue
            try:
                if str(node.get("type")) != "MODEL.CAL_PARAM":
                    continue
            except KeyError:
                continue
            if node.id is None:
                continue
            try:
                summary = repo.get_parameter_table_summary(node.id)
            except Exception:
                continue
            rows.append((summary.name, summary.category, summary.value_label, node.id))
            seen += 1
            if seen % seen_every == 0:
                if on_seen is not None:
                    on_seen(seen)
                else:
                    app = QApplication.instance()
                    if app is not None:
                        app.processEvents()
        if on_seen is not None:
            on_seen(seen)
        elif seen % seen_every != 0:
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        rows.sort(key=lambda row: row[0].lower())
        return rows

    def _refresh_table(
        self,
        *,
        dcm_table_progress: bool = False,
        dcm_imported_hint: int = 1,
    ) -> None:
        fr = self._dcm_import_status_frame
        t = self._dcm_import_write_total
        base = 3 * t
        gui_rng = self._dcm_table_bar_slots(t)
        collect_part = max(1, gui_rng // 2)

        def _on_collect_seen(seen: int) -> None:
            if (
                not dcm_table_progress
                or fr is None
                or not self._dcm_import_status_frame_in_bar
            ):
                return
            hint = max(1, dcm_imported_hint)
            frac = min(1.0, float(seen) / float(hint))
            fr.set_value(base + int((collect_part - 1) * frac))
            fr.set_message(f"DCM · Tabelle · Zeilen einlesen ({seen}) …")
            app = QApplication.instance()
            if app is not None:
                app.processEvents()

        try:
            rows = self._collect_rows(
                on_seen=_on_collect_seen if dcm_table_progress else None,
            )
        except ModuleNotFoundError as exc:
            QMessageBox.critical(
                self,
                "Missing dependency",
                f"{exc}\n\nInstall dependencies for synarius-apps, then restart ParaWiz.",
            )
            return

        if dcm_table_progress and fr is not None and self._dcm_import_status_frame_in_bar:
            fr.set_value(base + collect_part)
            fr.set_message("DCM · Tabelle · Zellen einfügen …")

        n = len(rows)
        self._table.setRowCount(n)
        pump_fill = max(1, n // 50)
        for row_idx, (name, ptype, value_repr, param_id) in enumerate(rows):
            it0 = QTableWidgetItem(name)
            it1 = QTableWidgetItem(ptype)
            it2 = QTableWidgetItem(value_repr)
            pid_s = str(param_id)
            for it in (it0, it1, it2):
                it.setData(Qt.ItemDataRole.UserRole, pid_s)
            self._table.setItem(row_idx, 0, it0)
            self._table.setItem(row_idx, 1, it1)
            self._table.setItem(row_idx, 2, it2)
            if dcm_table_progress and fr is not None and self._dcm_import_status_frame_in_bar and n > 0:
                if row_idx % pump_fill == 0 or row_idx == n - 1:
                    fill_prog = collect_part + int(
                        (gui_rng - collect_part) * (row_idx + 1) / float(n)
                    )
                    fr.set_value(base + min(fill_prog, gui_rng - 1))
                    app = QApplication.instance()
                    if app is not None:
                        app.processEvents()

        if dcm_table_progress and fr is not None and self._dcm_import_status_frame_in_bar:
            fr.set_value(base + gui_rng)
            fr.set_message("DCM · Tabelle fertig.")

        if not dcm_table_progress:
            self.statusBar().showMessage(f"{len(rows)} parameters loaded", 3000)

    def _on_parameter_table_double_clicked(self, row: int, col: int) -> None:
        if col != 2:
            return
        it = self._table.item(row, 0)
        if it is None:
            return
        pid_raw = it.data(Qt.ItemDataRole.UserRole)
        if not pid_raw:
            return
        try:
            pid = UUID(str(pid_raw))
        except ValueError:
            return
        try:
            rec = self._controller.model.parameter_runtime().repo.get_record(pid)
        except Exception:
            return
        from synariustools.tools.calmapwidget import (
            CalibrationMapData,
            create_calibration_map_viewer,
            supports_calibration_plot,
        )

        if not supports_calibration_plot(rec):
            QMessageBox.information(
                self,
                "ParaWiz",
                "Nur numerische Kenngrößen mit mindestens einer Dimension (Kennlinie, Kennfeld, Vektor) können geplottet werden.",
            )
            return
        data = CalibrationMapData.from_parameter_record(rec)
        shell = create_calibration_map_viewer(data, parent=self, embedded=True)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"ParaWiz — {rec.name}")
        dlg.setWindowIcon(self.windowIcon())
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(shell, 1)
        sh = shell.sizeHint()
        dlg.resize(sh.width(), sh.height())
        self._register_modeless_param_viewer(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
