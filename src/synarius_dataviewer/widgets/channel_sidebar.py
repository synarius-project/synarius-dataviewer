"""Left panel: search, checkable channel list, plot selection, drag-out for drops."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from synarius_core.io import TimeSeriesBundle, load_timeseries_file

from synarius_dataviewer.app.theme import channel_panel_stylesheet
from synariustools.tools.plotwidget.mime import MIME_CHANNEL


class _ChannelTableWidget(QTableWidget):
    """Channel table with drag-from-name-column (MIME channel payload)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(["Use", "Channel"])
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setShowGrid(True)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setDefaultSectionSize(18)
        self._drag_start = QPoint(0, 0)
        self._drag_row = -1

    def mousePressEvent(self, e: QMouseEvent) -> None:
        super().mousePressEvent(e)
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_start = e.position().toPoint()
        vp = self.viewport()
        if vp is None:
            self._drag_row = -1
            return
        vp_pos = vp.mapFrom(self, self._drag_start)
        idx = self.indexAt(vp_pos)
        if not idx.isValid() or self.isRowHidden(idx.row()):
            self._drag_row = -1
            return
        if idx.column() != 1:
            self._drag_row = -1
            return
        self._drag_row = idx.row()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if (
            self._drag_row >= 0
            and e.buttons() & Qt.MouseButton.LeftButton
            and (e.position().toPoint() - self._drag_start).manhattanLength() > 8
        ):
            name_item = self.item(self._drag_row, 1)
            if name_item is not None:
                name = name_item.text()
                mime = QMimeData()
                mime.setData(MIME_CHANNEL, QByteArray(name.encode("utf-8")))
                mime.setText(name)
                drag = QDrag(self)
                drag.setMimeData(mime)
                drag.exec(Qt.DropAction.CopyAction)
            self._drag_row = -1
        super().mouseMoveEvent(e)


class ChannelSidebar(QWidget):
    """Loads files, lists channels, search filter, plot selected, drag channel to viewer."""

    plot_selected_requested = Signal(list)
    bundle_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ChannelPanel")
        self.setStyleSheet(channel_panel_stylesheet())
        self._bundle: TimeSeriesBundle | None = None
        self._use_checks: list[QCheckBox] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        btn_open = QPushButton("Open file…")
        btn_open.clicked.connect(self._open_file)
        root.addWidget(btn_open)

        root.addWidget(QLabel("Search channels:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("substring…")
        self._search.textChanged.connect(self._apply_filter)
        root.addWidget(self._search)

        select_row = QWidget()
        select_lay = QHBoxLayout(select_row)
        select_lay.setContentsMargins(0, 0, 0, 0)
        self._btn_select_all = QPushButton("Select all (visible)")
        self._btn_select_all.clicked.connect(self._select_all_visible)
        select_lay.addWidget(self._btn_select_all)
        self._btn_clear_sel = QPushButton("Clear visible")
        self._btn_clear_sel.clicked.connect(self._clear_selection_visible)
        select_lay.addWidget(self._btn_clear_sel)
        root.addWidget(select_row)

        self._table = _ChannelTableWidget(self)
        root.addWidget(self._table, 1)

        btn_plot = QPushButton("Plot selected in active viewer")
        btn_plot.clicked.connect(self._emit_plot_selected)
        root.addWidget(btn_plot)

        btn_meta = QPushButton("Export metadata JSON…")
        btn_meta.clicked.connect(self._export_meta)
        root.addWidget(btn_meta)

    def bundle(self) -> TimeSeriesBundle | None:
        return self._bundle

    def set_bundle(self, bundle: TimeSeriesBundle | None) -> None:
        self._bundle = bundle
        self._table.setRowCount(0)
        self._use_checks = []
        if bundle is None:
            self.bundle_changed.emit(None)
            return
        names = sorted(bundle.channel_names(), key=str.lower)
        self._table.setRowCount(len(names))
        for row, name in enumerate(names):
            use_cell = QWidget(self._table)
            use_lay = QHBoxLayout(use_cell)
            use_lay.setContentsMargins(0, 0, 0, 0)
            use_lay.setSpacing(0)
            cb = QCheckBox(use_cell)
            use_lay.addStretch(1)
            use_lay.addWidget(cb, alignment=Qt.AlignmentFlag.AlignCenter)
            use_lay.addStretch(1)
            self._table.setCellWidget(row, 0, use_cell)
            self._use_checks.append(cb)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._table.setItem(row, 1, name_item)
        self._apply_filter()
        self.bundle_changed.emit(bundle)

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open time series",
            "",
            "Supported (*.csv *.parquet *.pq *.mf4 *.mdf *.dat);;All (*.*)",
        )
        if not path:
            return
        try:
            bundle = load_timeseries_file(path)
            self.set_bundle(bundle)
        except Exception as exc:
            QMessageBox.critical(self, "Open file", str(exc))

    def _apply_filter(self) -> None:
        q = self._search.text().strip().lower()
        for r in range(self._table.rowCount()):
            name_item = self._table.item(r, 1)
            name = name_item.text().lower() if name_item else ""
            self._table.setRowHidden(r, bool(q) and q not in name)

    def _select_all_visible(self) -> None:
        for r in range(self._table.rowCount()):
            if self._table.isRowHidden(r):
                continue
            if r < len(self._use_checks):
                self._use_checks[r].setChecked(True)

    def _clear_selection_visible(self) -> None:
        for r in range(self._table.rowCount()):
            if self._table.isRowHidden(r):
                continue
            if r < len(self._use_checks):
                self._use_checks[r].setChecked(False)

    def selected_channels(self) -> list[str]:
        out: list[str] = []
        for r in range(self._table.rowCount()):
            name_item = self._table.item(r, 1)
            if name_item is None:
                continue
            if r < len(self._use_checks) and self._use_checks[r].isChecked():
                out.append(name_item.text())
        return out

    def _emit_plot_selected(self) -> None:
        sel = self.selected_channels()
        if not sel:
            QMessageBox.information(self, "Plot", "Select at least one channel (checkbox).")
            return
        self.plot_selected_requested.emit(sel)

    def _export_meta(self) -> None:
        if self._bundle is None:
            QMessageBox.information(self, "Metadata", "Load a file first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save metadata", "", "JSON (*.json)")
        if not path:
            return
        try:
            self._bundle.save_metadata_json(path)
        except Exception as exc:
            QMessageBox.critical(self, "Metadata", str(exc))
