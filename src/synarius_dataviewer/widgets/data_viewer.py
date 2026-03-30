"""Multi-channel time-series plot: PyLinX-style black scope (QPixmap + QPainter, no pyqtgraph)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import numpy as np
from PySide6.QtCore import QEvent, QMimeData, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMdiSubWindow,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from synarius_dataviewer.app import theme
from synarius_dataviewer.app.svg_icons import icon_from_tinted_svg_file
from synarius_dataviewer.widgets.channel_sidebar import MIME_CHANNEL
from synarius_dataviewer.widgets.pixmap_scope import PixmapScopeWidget


def _find_mdi_subwindow(widget: QWidget) -> QMdiSubWindow | None:
    w: QWidget | None = widget
    while w is not None:
        if isinstance(w, QMdiSubWindow):
            return w
        w = w.parentWidget()
    return None


_COLOR_CYCLE = [
    "#00ff99",
    "#00c8ff",
    "#ffaa00",
    "#ff6699",
    "#cc77ff",
    "#eeff44",
    "#66ffcc",
    "#ff8844",
]


class DataViewerWidget(QWidget):
    """Plot widget with toolbar (Adjust, optional walking X window, side legend table, clear).

    Scope rendering uses :class:`PixmapScopeWidget` (single pixmap repaint per frame). Slider drags
    update the legend immediately via :signal:`PixmapScopeWidget.slider_positions_changed`.

    Real-time: call :meth:`set_channel_data` or :meth:`append_samples` from any thread only via Qt
    signals — for in-process Studio integration, call from the GUI thread or use
    ``QMetaObject.invokeMethod`` / a queued signal.
    """

    channel_drop_requested = Signal(str)
    _color_index: int
    _min_plot_width = 420
    _min_legend_width = 260
    _max_legend_width = 360

    def __init__(
        self,
        resolve_series: Callable[[str], tuple[np.ndarray, np.ndarray]],
        parent: QWidget | None = None,
        *,
        enable_walking_axis: bool = False,
        resolve_channel_unit: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._resolve_series = resolve_series
        self._resolve_channel_unit = resolve_channel_unit
        self._color_index = 0
        self._channel_pens: dict[str, QPen] = {}
        self._walk_span = 10.0
        self._legend_visible = True
        self._channel_legend_row: dict[str, int] = {}
        self._legend_split_saved = 380
        self._slider_cols_saved = 240
        self._highlighted_channels: set[str] = set()
        self._scope_window_saved_width: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        # Allow user to drag-resize boundary between scope and legend.
        self._splitter.setHandleWidth(6)

        plot_column = QWidget()
        plot_column_lay = QVBoxLayout(plot_column)
        plot_column_lay.setContentsMargins(0, 0, 0, 0)
        plot_column_lay.setSpacing(0)

        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setStyleSheet(theme.studio_toolbar_stylesheet())
        icons_dir = Path(__file__).resolve().parents[1] / "app" / "icons" / "toolbar"
        icon_fg = QColor(theme.STUDIO_TOOLBAR_FOREGROUND)

        self._walk_action = None

        self._scope_action = self._toolbar.addAction("Scope")
        self._scope_action.setIcon(
            icon_from_tinted_svg_file(icons_dir / "labplot-xy-plot-four-axes.svg", icon_fg)
        )
        self._scope_action.setCheckable(True)
        self._scope_action.setChecked(True)
        self._scope_action.setToolTip("Show or hide the oscilloscope area")
        self._scope_action.toggled.connect(self._on_scope_toggled)

        self._legend_action = self._toolbar.addAction("Legend")
        self._legend_action.setIcon(icon_from_tinted_svg_file(icons_dir / "legend.svg", icon_fg))
        self._legend_action.setCheckable(True)
        self._legend_action.setChecked(True)
        self._legend_action.setToolTip("Show or hide the signal list (adjusts window width)")
        self._legend_action.toggled.connect(self._on_legend_panel_toggled)

        self._slider_action = self._toolbar.addAction("Slider")
        self._slider_action.setIcon(icon_from_tinted_svg_file(icons_dir / "slider.svg", icon_fg))
        self._slider_action.setCheckable(True)
        self._slider_action.setToolTip("Show two vertical cursors (A/B); values appear in the legend columns")
        self._slider_action.toggled.connect(self._on_slider_toggled)

        act_adjust = self._toolbar.addAction("Adjust")
        act_adjust.setIcon(icon_from_tinted_svg_file(icons_dir / "adjust.svg", icon_fg))
        act_adjust.setToolTip("Autoscale X/Y (PyLinX-style Ctrl+A)")
        act_adjust.triggered.connect(self._on_adjust)
        self._adjust_action = act_adjust

        act_clear = self._toolbar.addAction("Clear")
        act_clear.setIcon(icon_from_tinted_svg_file(icons_dir / "clear.svg", icon_fg))
        act_clear.triggered.connect(self.clear_channels)

        if enable_walking_axis:
            self._walk_action = self._toolbar.addAction("Walking axis")
            self._walk_action.setCheckable(True)
            self._walk_action.setToolTip("Keep a rolling time window on the X axis")
            self._walk_action.toggled.connect(self._on_walk_toggled)

        layout.addWidget(self._toolbar)

        self._scope = PixmapScopeWidget()
        self._scope.slider_positions_changed.connect(self._refresh_slider_legend_values)
        plot_column_lay.addWidget(self._scope, 1)

        self._legend_panel = QWidget()
        self._legend_panel.setObjectName("LegendPanel")
        self._legend_panel.setMinimumWidth(260)
        self._legend_panel.setStyleSheet(theme.data_viewer_legend_panel_stylesheet())
        legend_lay = QVBoxLayout(self._legend_panel)
        legend_lay.setContentsMargins(0, 0, 0, 0)
        legend_lay.setSpacing(0)
        self._legend_table = QTableWidget(0, 6)
        self._legend_table.setHorizontalHeaderLabels(
            ["Color", "Signal Name", "Unit", "Slider A", "Slider B", "Difference"]
        )
        sig_hdr_item = self._legend_table.horizontalHeaderItem(1)
        if sig_hdr_item is not None:
            sig_hdr_item.setText("Signal Name\n")
            sig_hdr_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._legend_table.setAlternatingRowColors(True)
        self._legend_table.verticalHeader().setVisible(False)
        self._legend_table.setShowGrid(True)
        self._legend_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._legend_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._legend_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self._legend_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # "Signal Name" stays manually resizable by drag.
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._legend_table.setColumnWidth(0, 44)
        self._legend_table.setColumnWidth(1, 180)
        self._legend_table.setColumnWidth(2, 24)
        self._legend_table.setColumnWidth(3, 80)
        self._legend_table.setColumnWidth(4, 80)
        self._legend_table.setColumnWidth(5, 90)
        self._legend_table.verticalHeader().setDefaultSectionSize(18)
        self._legend_table.horizontalHeader().setStretchLastSection(True)
        # Allow a second line in header for the "Skip Namespace" checkbox.
        hdr.setFixedHeight(34)
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._skip_namespace_cb = QCheckBox("Skip Namespace", hdr.viewport())
        self._skip_namespace_cb.setChecked(False)
        self._skip_namespace_cb.setStyleSheet(
            "QCheckBox { color: #ffffff; background: transparent; spacing: 4px; font-size: 10px; }"
            "QCheckBox::indicator { width: 10px; height: 10px; "
            "border: 1px solid #d0d0d0; background: #2f2f2f; }"
            "QCheckBox::indicator:checked { background: #586cd4; border: 1px solid #ffffff; }"
        )
        self._skip_namespace_cb.toggled.connect(self._apply_legend_name_display)
        hdr.sectionResized.connect(lambda *_: self._position_skip_namespace_checkbox())
        hdr.sectionMoved.connect(lambda *_: self._position_skip_namespace_checkbox())
        QTimer.singleShot(0, self._position_skip_namespace_checkbox)
        for col in (3, 4, 5):
            self._legend_table.setColumnHidden(col, True)
        legend_lay.addWidget(self._legend_table)

        self._splitter.addWidget(plot_column)
        self._splitter.addWidget(self._legend_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([700, 300])
        self._splitter.splitterMoved.connect(lambda *_: self._enforce_splitter_bounds())

        layout.addWidget(self._splitter, 1)

        self.setAcceptDrops(True)
        self._scope.setAcceptDrops(True)
        self._scope.installEventFilter(self)

        self._empty_hint = QLabel(
            "Drag channel names here or use Plot selected in the sidebar.",
            self._scope,
        )
        self._empty_hint.setStyleSheet("color: #888; background: transparent;")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Keep startup splitter proportions; slider-column sizing runs when toggled.

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._position_hint()
        self._position_skip_namespace_checkbox()
        self._enforce_splitter_bounds()

    def _enforce_splitter_bounds(self) -> None:
        """Keep legend docked on the right and prevent oversizing beyond widget bounds."""
        total = self._splitter.width()
        if total <= 0:
            return
        if hasattr(self, "_scope_action") and not self._scope_action.isChecked():
            self._splitter.setSizes([0, total])
            return
        sizes = self._splitter.sizes()
        if len(sizes) < 2:
            return
        cur_legend = sizes[1]
        max_by_total = max(self._min_legend_width, total - self._min_plot_width)
        slider_on = hasattr(self, "_slider_action") and self._slider_action.isChecked()
        if slider_on:
            # In slider mode, legend may need extra width for A/B/Difference columns.
            max_legend = max_by_total
        else:
            max_legend = min(self._max_legend_width, max_by_total)
        if max_legend < self._min_legend_width:
            max_legend = self._min_legend_width
        clamped = max(self._min_legend_width, min(max_legend, cur_legend))
        if clamped != cur_legend:
            self._splitter.setSizes([max(self._min_plot_width, total - clamped), clamped])

    def _position_skip_namespace_checkbox(self) -> None:
        hdr = self._legend_table.horizontalHeader()
        if not self._skip_namespace_cb.isVisible():
            self._skip_namespace_cb.show()
        sec = 1  # "Signal Name"
        if sec >= self._legend_table.columnCount():
            return
        left = hdr.sectionViewportPosition(sec)
        # Place checkbox on a second line under "Signal Name", left-aligned.
        x = left + 6
        cb_h = self._skip_namespace_cb.sizeHint().height()
        y = max(14, hdr.viewport().height() - cb_h - 2)
        self._skip_namespace_cb.move(x, y)
        self._skip_namespace_cb.raise_()

    def _display_signal_name(self, raw_name: str) -> str:
        if not self._skip_namespace_cb.isChecked():
            return raw_name
        if "." not in raw_name:
            return raw_name
        # Hide optional namespace up to and including the LAST dot.
        return raw_name.rsplit(".", 1)[1]

    def _apply_legend_name_display(self) -> None:
        for row in range(self._legend_table.rowCount()):
            nm = self._legend_table.item(row, 1)
            if nm is None:
                continue
            raw = nm.data(Qt.ItemDataRole.UserRole)
            if isinstance(raw, str) and raw:
                nm.setText(self._display_signal_name(raw))
        self._position_skip_namespace_checkbox()

    def _position_hint(self) -> None:
        if self._empty_hint.isVisible():
            r = self._scope.rect()
            self._empty_hint.setGeometry(r.adjusted(20, 60, -20, -20))

    def _next_pen(self) -> QPen:
        c = QColor(_COLOR_CYCLE[self._color_index % len(_COLOR_CYCLE)])
        self._color_index += 1
        p = QPen(c)
        p.setWidthF(1.5)
        p.setCosmetic(True)
        return p

    def _on_legend_panel_toggled(self, checked: bool) -> None:
        if not checked and not self._scope_action.isChecked():
            self._legend_action.blockSignals(True)
            self._legend_action.setChecked(True)
            self._legend_action.blockSignals(False)
            return
        self._legend_visible = bool(checked)
        host = _find_mdi_subwindow(self)
        if host is not None:
            if checked:
                lw = max(200, self._legend_split_saved)
                self._legend_panel.setVisible(True)
                geo = host.geometry()
                host.setGeometry(geo.x(), geo.y(), geo.width() + lw, geo.height())
                tw = self._splitter.width()
                if tw <= 0:
                    tw = max(400, host.width())
                self._splitter.setSizes([max(120, tw - lw), lw])
            else:
                sizes = self._splitter.sizes()
                lw = sizes[1] if len(sizes) > 1 and sizes[1] > 0 else self._legend_split_saved
                self._legend_split_saved = max(200, lw)
                self._legend_panel.setVisible(False)
                geo = host.geometry()
                nw = max(host.minimumWidth(), geo.width() - self._legend_split_saved)
                host.setGeometry(geo.x(), geo.y(), nw, geo.height())
            return
        self._legend_panel.setVisible(self._legend_visible)

    def _on_scope_toggled(self, checked: bool) -> None:
        if not checked and not self._legend_action.isChecked():
            self._scope_action.blockSignals(True)
            self._scope_action.setChecked(True)
            self._scope_action.blockSignals(False)
            return
        show_scope = bool(checked)
        self._scope.setVisible(show_scope)
        self._adjust_action.setVisible(show_scope)
        self._slider_action.setVisible(show_scope)

        host = _find_mdi_subwindow(self)
        if not show_scope:
            # Collapse to legend-only mode and remember old overall width.
            if host is not None:
                self._scope_window_saved_width = host.geometry().width()
                sizes = self._splitter.sizes()
                scope_w = sizes[0] if len(sizes) > 0 else 0
                new_w = max(host.minimumWidth(), self._scope_window_saved_width - max(0, scope_w))
                host.setGeometry(host.x(), host.y(), new_w, host.height())
            self._splitter.setSizes([0, max(self._min_legend_width, self._legend_panel.width())])
            self._enforce_splitter_bounds()
            return

        # Restore normal splitter/window width when scope becomes visible again.
        self._enforce_splitter_bounds()
        if host is not None and self._scope_window_saved_width is not None:
            host.setGeometry(
                host.x(),
                host.y(),
                max(host.minimumWidth(), self._scope_window_saved_width),
                host.height(),
            )
            self._scope_window_saved_width = None

    def _on_slider_toggled(self, on: bool) -> None:
        self._scope.set_sliders_visible(on)
        self._set_slider_columns_visible(on)
        if not on:
            self._clear_slider_legend_cells()
        else:
            self._refresh_slider_legend_values()

    def _set_slider_columns_visible(self, visible: bool) -> None:
        prev_sizes = self._splitter.sizes()
        current_legend_w = prev_sizes[1] if len(prev_sizes) > 1 else self._legend_panel.width()
        for col in (3, 4, 5):
            self._legend_table.setColumnHidden(col, not visible)
        # Keep user-defined width for "Signal Name"; only auto-size non-name columns.
        self._legend_table.resizeColumnToContents(0)
        self._legend_table.setColumnWidth(2, 24)
        if visible:
            for col in (3, 4, 5):
                self._legend_table.resizeColumnToContents(col)
        legend_w = 0
        visible_cols = [c for c in range(self._legend_table.columnCount()) if not self._legend_table.isColumnHidden(c)]
        if 1 in visible_cols:
            legend_w = (
                self._legend_table.columnWidth(0)
                + self._legend_table.columnWidth(1)
                + self._legend_table.columnWidth(2)
            )
            if visible:
                legend_w += (
                    self._legend_table.columnWidth(3)
                    + self._legend_table.columnWidth(4)
                    + self._legend_table.columnWidth(5)
                )
        legend_w += 2 + self._legend_table.verticalScrollBar().sizeHint().width()
        legend_w = max(260, legend_w)
        if not visible:
            legend_w = min(legend_w, self._max_legend_width)
        self._legend_panel.setMinimumWidth(legend_w)
        self._legend_panel.setMaximumWidth(16_777_215)
        total_w = self._splitter.width()
        # During early init splitter width can be tiny/zero; avoid collapsing scope.
        if total_w > legend_w + 220:
            self._splitter.setSizes([max(220, total_w - legend_w), legend_w])
        self._enforce_splitter_bounds()

        host = _find_mdi_subwindow(self)
        if host is None:
            return
        if visible:
            self._slider_cols_saved = max(0, legend_w - max(self._min_legend_width, current_legend_w))
            g = host.geometry()
            host.setGeometry(g.x(), g.y(), g.width() + self._slider_cols_saved, g.height())
        else:
            g = host.geometry()
            nw = max(host.minimumWidth(), g.width() - self._slider_cols_saved)
            host.setGeometry(g.x(), g.y(), nw, g.height())

    def _slider_x_positions(self) -> tuple[float | None, float | None]:
        return self._scope.slider_data_x_positions()

    @staticmethod
    def _fmt_measure(v: float | None) -> str:
        if v is None or not np.isfinite(v):
            return "—"
        return f"{v:.6g}"

    def _interp_channel_at(self, name: str, xq: float) -> float | None:
        pair = self._scope.get_series(name)
        if pair is None:
            return None
        txa, tya = pair
        if len(txa) == 0:
            return None
        txa = np.asarray(txa, dtype=np.float64).ravel()
        tya = np.asarray(tya, dtype=np.float64).ravel()
        if txa.size != tya.size:
            return None
        if not np.all(np.diff(txa) >= 0):
            order = np.argsort(txa)
            txa = txa[order]
            tya = tya[order]
        if xq < txa[0] or xq > txa[-1]:
            return None
        return float(np.interp(xq, txa, tya))

    def _set_legend_slider_cells(self, row: int, ya: float | None, yb: float | None, diff: float | None) -> None:
        for col, val in zip((3, 4, 5), (ya, yb, diff), strict=True):
            it = self._legend_table.item(row, col)
            if it is None:
                it = QTableWidgetItem()
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self._legend_table.setItem(row, col, it)
            it.setText(self._fmt_measure(val))

    def _clear_slider_legend_cells(self) -> None:
        for row in range(self._legend_table.rowCount()):
            self._set_legend_slider_cells(row, None, None, None)

    def _refresh_slider_legend_values(self) -> None:
        if not self._slider_action.isChecked():
            self._clear_slider_legend_cells()
            return
        xa, xb = self._slider_x_positions()
        if xa is None or xb is None:
            self._clear_slider_legend_cells()
            return
        for name, row in self._channel_legend_row.items():
            ya = self._interp_channel_at(name, xa)
            yb = self._interp_channel_at(name, xb)
            diff: float | None = None
            if ya is not None and yb is not None:
                diff = float(yb - ya)
            self._set_legend_slider_cells(row, ya, yb, diff)

    def _register_legend_row(self, name: str, pen: QPen) -> None:
        if name in self._channel_legend_row:
            return
        row = self._legend_table.rowCount()
        self._legend_table.insertRow(row)
        c = pen.color()
        # Color cell: full background in signal color, centered checkbox on top.
        cell = QWidget()
        cell.setStyleSheet(f"background-color: {c.name(QColor.NameFormat.HexRgb)};")
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        cb = QCheckBox(cell)
        cb.setToolTip("Highlight this signal")
        lay.addStretch(1)
        lay.addWidget(cb, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addStretch(1)
        self._legend_table.setCellWidget(row, 0, cell)

        def _on_cb_toggled(checked: bool, chan: str = name) -> None:
            self._set_channel_highlight(chan, checked)

        cb.toggled.connect(_on_cb_toggled)

        nm = QTableWidgetItem(self._display_signal_name(name))
        nm.setData(Qt.ItemDataRole.UserRole, name)
        nm.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        nm.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self._legend_table.setItem(row, 1, nm)
        unit_text = ""
        if self._resolve_channel_unit is not None:
            try:
                unit_text = self._resolve_channel_unit(name) or ""
            except Exception:
                unit_text = ""
        u_item = QTableWidgetItem(unit_text)
        u_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._legend_table.setItem(row, 2, u_item)
        for col in (3, 4, 5):
            s_item = QTableWidgetItem("—")
            s_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._legend_table.setItem(row, col, s_item)
        self._channel_legend_row[name] = row
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()

    def _remove_legend_row(self, name: str) -> None:
        row = self._channel_legend_row.pop(name, None)
        if row is None:
            return
        self._legend_table.removeRow(row)
        for key, r in list(self._channel_legend_row.items()):
            if r > row:
                self._channel_legend_row[key] = r - 1

    def _clear_legend_rows(self) -> None:
        self._legend_table.setRowCount(0)
        self._channel_legend_row.clear()
        self._highlighted_channels.clear()

    def add_channel(self, name: str) -> None:
        if name in self._channel_pens:
            return
        tx, ty = self._resolve_series(name)
        if len(tx) == 0:
            return
        pen = self._next_pen()
        self._channel_pens[name] = pen
        self._scope.set_series(name, tx, ty, pen)
        self._register_legend_row(name, pen)
        self._empty_hint.hide()

    def remove_channel(self, name: str) -> None:
        if name not in self._channel_pens:
            return
        self._channel_pens.pop(name, None)
        self._highlighted_channels.discard(name)
        self._scope.remove_series(name)
        self._remove_legend_row(name)
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()
        if not self._channel_pens:
            self._empty_hint.show()
            self._position_hint()

    def clear_channels(self) -> None:
        self._channel_pens.clear()
        self._scope.clear_series()
        self._color_index = 0
        self._clear_legend_rows()
        self._highlighted_channels.clear()
        self._empty_hint.show()
        self._position_hint()
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()

    def set_channel_data(self, name: str, t: np.ndarray, y: np.ndarray) -> None:
        """Replace curve arrays (full redraw; preferred for simulation ticks)."""
        t = np.asarray(t, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if name not in self._channel_pens:
            pen = self._next_pen()
            self._channel_pens[name] = pen
            self._register_legend_row(name, pen)
            self._empty_hint.hide()
        pen = self._channel_pens[name]
        self._scope.set_series(name, t, y, pen)
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()

    def append_samples(
        self,
        name: str,
        t_new: np.ndarray,
        y_new: np.ndarray,
        max_points: int = 200_000,
    ) -> None:
        """Append samples in O(n) by rebuilding a bounded buffer (GUI thread)."""
        t_new = np.asarray(t_new, dtype=np.float64).ravel()
        y_new = np.asarray(y_new, dtype=np.float64).ravel()
        if len(t_new) == 0:
            return
        if name in self._channel_pens:
            pair = self._scope.get_series(name)
            pen = self._channel_pens[name]
            if pair is None or len(pair[0]) == 0:
                t_all, y_all = t_new, y_new
            else:
                tx, ty = pair
                t_all = np.concatenate([tx, t_new])
                y_all = np.concatenate([ty, y_new])
            if len(t_all) > max_points:
                cut = len(t_all) - max_points
                t_all = t_all[cut:]
                y_all = y_all[cut:]
            self._scope.set_series(name, t_all, y_all, pen)
            if self._slider_action.isChecked():
                self._refresh_slider_legend_values()
        else:
            self.set_channel_data(name, t_new, y_new)

    def _set_channel_highlight(self, name: str, checked: bool) -> None:
        """Highlight a channel: bold legend row + thicker pen."""
        row = self._channel_legend_row.get(name)
        if row is None:
            return
        if checked:
            self._highlighted_channels.add(name)
        else:
            self._highlighted_channels.discard(name)

        # Bold/unbold the entire row.
        for col in range(self._legend_table.columnCount()):
            item = self._legend_table.item(row, col)
            if item is None:
                continue
            f = item.font()
            f.setBold(checked)
            item.setFont(f)

        # Adjust pen width for the corresponding curve.
        pen = self._channel_pens.get(name)
        if pen is not None:
            pen.setWidthF(4.0 if checked else 1.5)
            # Re-render scope to apply new pen width.
            self._scope.refresh_pixmap()

    def _on_adjust(self) -> None:
        self._scope.auto_range()

    def _on_walk_toggled(self, on: bool) -> None:
        if self._walk_action is None:
            return
        self._scope.set_walking_axis(on, self._walk_span)

    def _mime_names(self, md: QMimeData) -> list[str]:
        names: list[str] = []
        if md.hasFormat(MIME_CHANNEL):
            raw = bytes(md.data(MIME_CHANNEL)).decode("utf-8")
            names = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if not names and raw.strip():
                names = [raw.strip()]
        elif md.hasText():
            names = [ln.strip() for ln in md.text().splitlines() if ln.strip()]
            if not names and md.text().strip():
                names = [md.text().strip()]
        return names

    def _can_accept_mime(self, md: QMimeData) -> bool:
        return md.hasFormat(MIME_CHANNEL) or md.hasText()

    def _apply_drop(self, md: QMimeData) -> None:
        for n in self._mime_names(md):
            try:
                self.add_channel(n)
            except KeyError:
                self.channel_drop_requested.emit(n)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self._scope:
            return super().eventFilter(watched, event)
        et = event.type()
        if et == QEvent.Type.DragEnter:
            ev = cast(QDragEnterEvent, event)
            if self._can_accept_mime(ev.mimeData()):
                ev.acceptProposedAction()
            else:
                ev.ignore()
            return True
        if et == QEvent.Type.DragMove:
            ev = cast(QDragMoveEvent, event)
            if self._can_accept_mime(ev.mimeData()):
                ev.acceptProposedAction()
            else:
                ev.ignore()
            return True
        if et == QEvent.Type.Drop:
            ev = cast(QDropEvent, event)
            self._apply_drop(ev.mimeData())
            ev.acceptProposedAction()
            return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._can_accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._can_accept_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        self._apply_drop(event.mimeData())
        event.acceptProposedAction()


class DataViewerShell(QWidget):
    """Toolbar + :class:`DataViewerWidget` for embedding in an ``QMdiSubWindow``."""

    def __init__(
        self,
        resolve_series: Callable[[str], tuple[np.ndarray, np.ndarray]],
        parent: QWidget | None = None,
        *,
        enable_walking_axis: bool = False,
        resolve_channel_unit: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = DataViewerWidget(
            resolve_series,
            self,
            enable_walking_axis=enable_walking_axis,
            resolve_channel_unit=resolve_channel_unit,
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._viewer)

    @property
    def viewer(self) -> DataViewerWidget:
        return self._viewer
