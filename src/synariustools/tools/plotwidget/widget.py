"""Qt plot widget + shell (scope + legend + toolbar)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, cast

import numpy as np
from PySide6.QtCore import QEvent, QMimeData, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMdiSubWindow,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from synarius_core.recording import export_recording_buffers

from synariustools.tools.plotwidget.channel_registry import ChannelRegistry
from synariustools.tools.plotwidget.datasource import (
    TimeSeriesDataSource,
    as_data_source,
)
from synariustools.tools.plotwidget.mime import MIME_CHANNEL
from synariustools.tools.plotwidget.modes import PlotViewerMode, resolve_mode
from synariustools.tools.plotwidget.pixmap_scope import PixmapScopeWidget
from synariustools.tools.plotwidget.plot_theme import (
    DATAVIEWER_LEGEND_SIGNAL_HIDDEN_TEXT,
    DATAVIEWER_LEGEND_SIGNAL_TEXT,
    STUDIO_TOOLBAR_FOREGROUND,
    data_viewer_legend_panel_stylesheet,
    studio_toolbar_stylesheet,
)
from synariustools.tools.plotwidget.series_math import (
    append_merge,
    fmt_measure,
    interp_y_at_x,
    latest_y,
)
from synariustools.tools.plotwidget.svg_icons import icon_from_tinted_svg_file


def _find_window_host(widget: QWidget) -> QWidget | None:
    w: QWidget | None = widget
    while w is not None:
        if isinstance(w, QMdiSubWindow):
            return w
        w = w.parentWidget()
    # Fallback für Nicht-MDI-Hosts (z.B. Studio öffnet DataViewer in QDialog).
    try:
        top = widget.window()
    except Exception:
        top = None
    return top if isinstance(top, QWidget) else None


def _legend_table_sum_column_widths(table: QTableWidget, columns: Sequence[int]) -> int:
    return sum(table.columnWidth(int(c)) for c in columns)


def _dataviewer_legend_width_columns(
    table: QTableWidget,
    *,
    col_color: int,
    col_name: int,
    col_value: int | None,
    show_value_column: bool,
    col_unit: int,
    col_slider_a: int,
    col_slider_b: int,
    col_slider_diff: int,
    slider_columns_visible: bool,
) -> list[int]:
    """Columns that contribute to the legend panel width (empty if the name column is hidden)."""
    visible_cols = {c for c in range(table.columnCount()) if not table.isColumnHidden(c)}
    if col_name not in visible_cols:
        return []
    cols = [col_color, col_name]
    if show_value_column and col_value is not None:
        cols.append(col_value)
    cols.append(col_unit)
    if slider_columns_visible:
        cols.extend((col_slider_a, col_slider_b, col_slider_diff))
    return cols


def _dataviewer_legend_panel_content_width(table: QTableWidget, width_columns: Sequence[int]) -> int:
    """Sum of column widths plus scrollbar gutter (matches DataViewer legend sizing)."""
    return _legend_table_sum_column_widths(table, width_columns) + 2 + table.verticalScrollBar().sizeHint().width()


class DataViewerWidget(QWidget):
    """Multi-channel plot: toolbar, :class:`PixmapScopeWidget`, optional legend table."""

    channel_drop_requested = Signal(str)
    recording_saved = Signal(str)

    def __init__(
        self,
        data_source: TimeSeriesDataSource | Callable[[str], tuple[np.ndarray, np.ndarray]],
        parent: QWidget | None = None,
        *,
        enable_walking_axis: bool = False,
        resolve_channel_unit: Callable[[str], str] | None = None,
        mode: PlotViewerMode | Literal["static", "dynamic"] = "static",
        legend_visible_at_start: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self._data_source = as_data_source(data_source, resolve_channel_unit=resolve_channel_unit)
        self._mode_cfg = resolve_mode(mode, legend_visible_at_start=legend_visible_at_start)
        self._registry = ChannelRegistry()
        self._walk_span = 10.0
        self._legend_visible = self._mode_cfg.legend_visible_by_default
        self._channel_legend_row: dict[str, int] = {}
        self._channel_name_labels: dict[str, QLabel] = {}
        self._legend_split_saved = self._mode_cfg.legend_split_saved
        self._slider_cols_saved = 240
        self._slider_restore_host_geometry: tuple[int, int, int, int] | None = None
        self._slider_restore_splitter_sizes: tuple[int, int] | None = None
        self._slider_restore_legend_width: int | None = None
        self._highlighted_channels: set[str] = set()
        self._scope_window_saved_width: int | None = None
        self._save_last_dir: Path | None = None
        self._save_last_basename: str | None = None
        self._save_last_format: str = "mdf"

        self._min_plot_width = self._mode_cfg.min_plot_width
        self._min_legend_width = self._mode_cfg.min_legend_width
        self._max_legend_width = self._mode_cfg.max_legend_width

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(6)

        plot_column = QWidget()
        plot_column_lay = QVBoxLayout(plot_column)
        plot_column_lay.setContentsMargins(0, 0, 0, 0)
        plot_column_lay.setSpacing(0)

        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setStyleSheet(studio_toolbar_stylesheet())
        icons_dir = Path(__file__).resolve().parent / "icons" / "toolbar"
        icon_fg = QColor(STUDIO_TOOLBAR_FOREGROUND)

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
        self._legend_action.setChecked(self._mode_cfg.legend_visible_by_default)
        self._legend_action.setToolTip("Show or hide the signal list (adjusts window width)")
        self._legend_action.toggled.connect(self._on_legend_panel_toggled)

        self._slider_action = self._toolbar.addAction("Slider")
        self._slider_action.setIcon(icon_from_tinted_svg_file(icons_dir / "slider.svg", icon_fg))
        self._slider_action.setCheckable(True)
        self._slider_action.setToolTip("Show two vertical cursors (A/B); values appear in the legend columns")
        self._slider_action.toggled.connect(self._on_slider_toggled)

        act_adjust = self._toolbar.addAction("Adjust")
        act_adjust.setIcon(icon_from_tinted_svg_file(icons_dir / "adjust.svg", icon_fg))
        act_adjust.setToolTip("Autoscale X/Y (Ctrl+A)")
        act_adjust.triggered.connect(self._on_adjust)
        self._adjust_action = act_adjust

        act_save = self._toolbar.addAction("Save")
        studio_save_icon = (
            Path(__file__).resolve().parents[5]
            / "synarius-studio"
            / "src"
            / "synarius_studio"
            / "icons"
            / "document-save-symbolic.svg"
        )
        if studio_save_icon.exists():
            act_save.setIcon(icon_from_tinted_svg_file(studio_save_icon, icon_fg))
        act_save.setToolTip("Save visible channels")
        act_save.triggered.connect(self._on_save_visible_channels)
        self._save_action = act_save

        if self._mode_cfg.show_clear_action:
            act_clear = self._toolbar.addAction("Clear")
            act_clear.setIcon(icon_from_tinted_svg_file(icons_dir / "clear.svg", icon_fg))
            act_clear.triggered.connect(self.clear_channels)

        if enable_walking_axis:
            self._walk_action = self._toolbar.addAction("Walking axis")
            self._walk_action.setIcon(
                icon_from_tinted_svg_file(icons_dir / "walkingAxis.svg", icon_fg)
            )
            self._walk_action.setCheckable(True)
            self._walk_action.setToolTip("Keep a rolling time window on the X axis")
            self._walk_action.toggled.connect(self._on_walk_toggled)

        layout.addWidget(self._toolbar)

        self._scope = PixmapScopeWidget()
        self._scope.slider_positions_changed.connect(self._refresh_slider_legend_values)
        plot_column_lay.addWidget(self._scope, 1)

        self._legend_panel = QWidget()
        self._legend_panel.setObjectName("LegendPanel")
        self._legend_panel.setMinimumWidth(self._min_legend_width)
        self._legend_panel.setStyleSheet(data_viewer_legend_panel_stylesheet())
        legend_lay = QVBoxLayout(self._legend_panel)
        legend_lay.setContentsMargins(0, 0, 0, 0)
        legend_lay.setSpacing(0)

        self._col_color = 0
        self._col_name = 1
        self._col_value = 2 if self._mode_cfg.show_value_column else None
        self._col_unit = 3 if self._mode_cfg.show_value_column else 2
        self._col_slider_a = self._col_unit + 1
        self._col_slider_b = self._col_unit + 2
        self._col_slider_diff = self._col_unit + 3
        col_count = self._col_slider_diff + 1
        headers = ["Color", "Signal Name"]
        if self._mode_cfg.show_value_column:
            headers.append("Value")
        headers.extend(["Unit", "Slider A", "Slider B", "Difference"])
        self._legend_table = QTableWidget(0, col_count)
        self._legend_table.setHorizontalHeaderLabels(headers)
        sig_hdr_item = self._legend_table.horizontalHeaderItem(self._col_name)
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
        hdr.setSectionResizeMode(self._col_name, QHeaderView.ResizeMode.Interactive)
        self._legend_table.setColumnWidth(0, 44)
        self._legend_table.setColumnWidth(self._col_name, 180)
        if self._mode_cfg.show_value_column and self._col_value is not None:
            self._legend_table.setColumnWidth(self._col_value, 84)
        self._legend_table.setColumnWidth(self._col_unit, 24)
        self._legend_table.setColumnWidth(self._col_slider_a, 80)
        self._legend_table.setColumnWidth(self._col_slider_b, 80)
        self._legend_table.setColumnWidth(self._col_slider_diff, 90)
        self._legend_table.verticalHeader().setDefaultSectionSize(18)
        self._legend_table.horizontalHeader().setStretchLastSection(True)
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
        for col in (self._col_slider_a, self._col_slider_b, self._col_slider_diff):
            self._legend_table.setColumnHidden(col, True)
        legend_lay.addWidget(self._legend_table)

        self._splitter.addWidget(plot_column)
        self._splitter.addWidget(self._legend_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([700, 300])
        self._splitter.splitterMoved.connect(lambda *_: self._enforce_splitter_bounds())
        self._legend_panel.setVisible(self._legend_action.isChecked())

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

    def _pen_for_channel(self, name: str) -> QPen | None:
        st = self._registry.style(name)
        if st is None:
            return None
        p = QPen(QColor(st.color_hex))
        p.setWidthF(st.pen_width)
        p.setCosmetic(True)
        return p

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._position_hint()
        self._position_skip_namespace_checkbox()
        self._enforce_splitter_bounds()

    def _enforce_splitter_bounds(self) -> None:
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
        sec = self._col_name
        if sec >= self._legend_table.columnCount():
            return
        left = hdr.sectionViewportPosition(sec)
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
        return raw_name.rsplit(".", 1)[1]

    def _apply_legend_name_display(self) -> None:
        for ch_name, lbl in self._channel_name_labels.items():
            lbl.setText(self._display_signal_name(ch_name))
        self._position_skip_namespace_checkbox()

    def _position_hint(self) -> None:
        if self._empty_hint.isVisible():
            r = self._scope.rect()
            self._empty_hint.setGeometry(r.adjusted(20, 60, -20, -20))

    def _on_legend_panel_toggled(self, checked: bool) -> None:
        if not checked and not self._scope_action.isChecked():
            self._legend_action.blockSignals(True)
            self._legend_action.setChecked(True)
            self._legend_action.blockSignals(False)
            return
        self._legend_visible = bool(checked)
        host = _find_window_host(self)
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

        host = _find_window_host(self)
        if not show_scope:
            if host is not None:
                self._scope_window_saved_width = host.geometry().width()
                sizes = self._splitter.sizes()
                scope_w = sizes[0] if len(sizes) > 0 else 0
                new_w = max(host.minimumWidth(), self._scope_window_saved_width - max(0, scope_w))
                host.setGeometry(host.x(), host.y(), new_w, host.height())
            self._splitter.setSizes([0, max(self._min_legend_width, self._legend_panel.width())])
            self._enforce_splitter_bounds()
            return

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

    def _maybe_snapshot_slider_restore_state(
        self,
        visible: bool,
        prev_sizes: list[int],
        current_legend_w: int,
        host_geo_before: QRect | None,
    ) -> None:
        if not visible or self._slider_restore_splitter_sizes is not None or len(prev_sizes) <= 1:
            return
        self._slider_restore_splitter_sizes = (int(prev_sizes[0]), int(prev_sizes[1]))
        self._slider_restore_legend_width = int(current_legend_w)
        if host_geo_before is not None:
            g = host_geo_before
            self._slider_restore_host_geometry = (
                int(g.x()),
                int(g.y()),
                int(g.width()),
                int(g.height()),
            )

    def _resize_legend_columns_for_slider_mode(self, slider_visible: bool) -> None:
        for col in (self._col_slider_a, self._col_slider_b, self._col_slider_diff):
            self._legend_table.setColumnHidden(col, not slider_visible)
        self._legend_table.resizeColumnToContents(0)
        self._legend_table.setColumnWidth(self._col_unit, 24)
        if slider_visible:
            for col in (self._col_slider_a, self._col_slider_b, self._col_slider_diff):
                self._legend_table.resizeColumnToContents(col)
        if self._mode_cfg.show_value_column and self._col_value is not None:
            self._legend_table.resizeColumnToContents(self._col_value)

    def _legend_panel_target_width_for_slider_state(self, slider_visible: bool) -> int:
        width_cols = _dataviewer_legend_width_columns(
            self._legend_table,
            col_color=self._col_color,
            col_name=self._col_name,
            col_value=self._col_value,
            show_value_column=self._mode_cfg.show_value_column,
            col_unit=self._col_unit,
            col_slider_a=self._col_slider_a,
            col_slider_b=self._col_slider_b,
            col_slider_diff=self._col_slider_diff,
            slider_columns_visible=slider_visible,
        )
        legend_w = _dataviewer_legend_panel_content_width(self._legend_table, width_cols)
        legend_w = max(self._min_legend_width, legend_w)
        if not slider_visible:
            legend_w = min(legend_w, self._max_legend_width)
            if self._slider_restore_legend_width is not None:
                legend_w = int(self._slider_restore_legend_width)
        return legend_w

    def _apply_legend_panel_min_width_and_splitter(self, legend_w: int) -> None:
        self._legend_panel.setMinimumWidth(legend_w)
        self._legend_panel.setMaximumWidth(16_777_215)
        total_w = self._splitter.width()
        if total_w > legend_w + 220:
            self._splitter.setSizes([max(220, total_w - legend_w), legend_w])
        self._enforce_splitter_bounds()

    def _clear_slider_restore_snapshots(self) -> None:
        self._slider_restore_splitter_sizes = None
        self._slider_restore_legend_width = None
        self._slider_restore_host_geometry = None

    def _adjust_host_for_slider_columns(
        self,
        slider_visible: bool,
        host: QWidget | None,
        current_legend_w: int,
        legend_w: int,
    ) -> None:
        if host is None:
            if not slider_visible:
                self._clear_slider_restore_snapshots()
            return
        if slider_visible:
            self._slider_cols_saved = max(0, legend_w - max(self._min_legend_width, current_legend_w))
            g = host.geometry()
            host.setGeometry(g.x(), g.y(), g.width() + self._slider_cols_saved, g.height())
            return
        restored = False
        if self._slider_restore_splitter_sizes is not None:
            self._splitter.setSizes(
                [int(self._slider_restore_splitter_sizes[0]), int(self._slider_restore_splitter_sizes[1])]
            )
            restored = True
        if self._slider_restore_host_geometry is not None:
            x, y, w, h = self._slider_restore_host_geometry
            host.setGeometry(int(x), int(y), max(host.minimumWidth(), int(w)), int(h))
            restored = True
        if not restored:
            g = host.geometry()
            nw = max(host.minimumWidth(), g.width() - self._slider_cols_saved)
            host.setGeometry(g.x(), g.y(), nw, g.height())
        self._clear_slider_restore_snapshots()

    def _set_slider_columns_visible(self, visible: bool) -> None:
        prev_sizes = self._splitter.sizes()
        current_legend_w = prev_sizes[1] if len(prev_sizes) > 1 else self._legend_panel.width()
        host = _find_window_host(self)
        host_geo_before = host.geometry() if host is not None else None

        self._maybe_snapshot_slider_restore_state(visible, prev_sizes, current_legend_w, host_geo_before)
        self._resize_legend_columns_for_slider_mode(visible)
        legend_w = self._legend_panel_target_width_for_slider_state(visible)
        self._apply_legend_panel_min_width_and_splitter(legend_w)
        self._adjust_host_for_slider_columns(visible, host, current_legend_w, legend_w)

    def _slider_x_positions(self) -> tuple[float | None, float | None]:
        return self._scope.slider_data_x_positions()

    def _interp_channel_at(self, name: str, xq: float) -> float | None:
        pair = self._scope.get_series(name)
        if pair is None:
            return None
        txa, tya = pair
        return interp_y_at_x(txa, tya, xq)

    def _set_legend_slider_cells(self, row: int, ya: float | None, yb: float | None, diff: float | None) -> None:
        for col, val in zip(
            (self._col_slider_a, self._col_slider_b, self._col_slider_diff),
            (ya, yb, diff),
            strict=True,
        ):
            it = self._legend_table.item(row, col)
            if it is None:
                it = QTableWidgetItem()
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self._legend_table.setItem(row, col, it)
            it.setText(fmt_measure(val))

    def _refresh_latest_value_cell(self, name: str) -> None:
        if not self._mode_cfg.show_value_column or self._col_value is None:
            return
        row = self._channel_legend_row.get(name)
        if row is None:
            return
        it = self._legend_table.item(row, self._col_value)
        if it is None:
            it = QTableWidgetItem("—")
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._legend_table.setItem(row, self._col_value, it)
        pair = self._scope.get_series(name)
        if pair is None or len(pair[0]) == 0:
            it.setText("—")
            return
        ly = latest_y(pair[1])
        it.setText(fmt_measure(ly))
        self._sync_legend_row_text_colors(name)

    def _clear_slider_legend_cells(self) -> None:
        for row in range(self._legend_table.rowCount()):
            self._set_legend_slider_cells(row, None, None, None)
        for ch in self._channel_legend_row:
            self._sync_legend_row_text_colors(ch)

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
            self._sync_legend_row_text_colors(name)

    def _sync_legend_row_text_colors(self, name: str) -> None:
        """Black row text when the trace is shown; gray when hidden via the scope checkbox."""
        row = self._channel_legend_row.get(name)
        if row is None:
            return
        visible = self._scope.is_series_visible(name)
        hex_fg = DATAVIEWER_LEGEND_SIGNAL_TEXT if visible else DATAVIEWER_LEGEND_SIGNAL_HIDDEN_TEXT
        brush = QBrush(QColor(hex_fg))
        nl = self._channel_name_labels.get(name)
        if nl is not None:
            nl.setStyleSheet(f"color: {hex_fg}; background: transparent;")
        cols: list[int] = [self._col_unit]
        if self._mode_cfg.show_value_column and self._col_value is not None:
            cols.append(self._col_value)
        cols.extend((self._col_slider_a, self._col_slider_b, self._col_slider_diff))
        for col in cols:
            it = self._legend_table.item(row, col)
            if it is not None:
                it.setForeground(brush)

    def _register_legend_row(self, name: str, pen: QPen) -> None:
        if name in self._channel_legend_row:
            return
        row = self._legend_table.rowCount()
        self._legend_table.insertRow(row)
        c = pen.color()
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

        name_cell = QWidget()
        name_lay = QHBoxLayout(name_cell)
        name_lay.setContentsMargins(4, 0, 2, 0)
        name_lay.setSpacing(4)
        name_label = QLabel(self._display_signal_name(name))
        name_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        name_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        name_label.setStyleSheet(
            f"color: {DATAVIEWER_LEGEND_SIGNAL_TEXT}; background: transparent;"
        )
        name_lay.addWidget(name_label, 1)
        vis_cb = QCheckBox(name_cell)
        vis_cb.setChecked(True)
        vis_cb.setToolTip("Show or hide trace on scope")
        name_lay.addWidget(
            vis_cb, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._legend_table.setCellWidget(row, self._col_name, name_cell)
        self._channel_name_labels[name] = name_label

        def _on_vis_toggled(checked: bool, chan: str = name) -> None:
            self._scope.set_series_visible(chan, checked)
            self._sync_legend_row_text_colors(chan)

        vis_cb.toggled.connect(_on_vis_toggled)
        if self._mode_cfg.show_value_column and self._col_value is not None:
            v_item = QTableWidgetItem("—")
            v_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._legend_table.setItem(row, self._col_value, v_item)
        unit_text = ""
        try:
            unit_text = self._data_source.channel_unit(name) or ""
        except Exception:
            unit_text = ""
        u_item = QTableWidgetItem(unit_text)
        u_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._legend_table.setItem(row, self._col_unit, u_item)
        for col in (self._col_slider_a, self._col_slider_b, self._col_slider_diff):
            s_item = QTableWidgetItem("—")
            s_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._legend_table.setItem(row, col, s_item)
        self._channel_legend_row[name] = row
        self._refresh_latest_value_cell(name)
        self._sync_legend_row_text_colors(name)
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()

    def _remove_legend_row(self, name: str) -> None:
        row = self._channel_legend_row.pop(name, None)
        if row is None:
            return
        self._channel_name_labels.pop(name, None)
        self._legend_table.removeRow(row)
        for key, r in list(self._channel_legend_row.items()):
            if r > row:
                self._channel_legend_row[key] = r - 1

    def _clear_legend_rows(self) -> None:
        self._legend_table.setRowCount(0)
        self._channel_legend_row.clear()
        self._channel_name_labels.clear()
        self._highlighted_channels.clear()

    def add_channel(self, name: str) -> None:
        if name in self._registry:
            return
        tx, ty = self._data_source.get_series(name)
        self._registry.add(name)
        pen = self._pen_for_channel(name)
        if pen is None:
            self._registry.remove(name)
            return
        self._scope.set_series(name, tx, ty, pen)
        self._register_legend_row(name, pen)
        self._empty_hint.hide()

    def remove_channel(self, name: str) -> None:
        if name not in self._registry:
            return
        self._registry.remove(name)
        self._highlighted_channels.discard(name)
        self._scope.remove_series(name)
        self._remove_legend_row(name)
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()
        if not self._registry.names():
            self._empty_hint.show()
            self._position_hint()

    def clear_channels(self) -> None:
        self._registry.clear()
        self._scope.clear_series()
        self._clear_legend_rows()
        self._highlighted_channels.clear()
        self._empty_hint.show()
        self._position_hint()
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()

    def set_channel_data(self, name: str, t: np.ndarray, y: np.ndarray) -> None:
        t = np.asarray(t, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if name not in self._registry:
            self._registry.add(name)
            pen = self._pen_for_channel(name)
            if pen is None:
                self._registry.remove(name)
                return
            self._register_legend_row(name, pen)
            self._empty_hint.hide()
        pen = self._pen_for_channel(name)
        if pen is None:
            return
        self._scope.set_series(name, t, y, pen)
        self._refresh_latest_value_cell(name)
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()

    def append_samples(
        self,
        name: str,
        t_new: np.ndarray,
        y_new: np.ndarray,
        max_points: int = 200_000,
    ) -> None:
        t_new = np.asarray(t_new, dtype=np.float64).ravel()
        y_new = np.asarray(y_new, dtype=np.float64).ravel()
        if len(t_new) == 0:
            return
        if name in self._registry:
            pair = self._scope.get_series(name)
            pen = self._pen_for_channel(name)
            if pen is None:
                return
            if pair is None or len(pair[0]) == 0:
                t_all, y_all = t_new, y_new
            else:
                tx, ty = pair
                t_all, y_all = append_merge(tx, ty, t_new, y_new, max_points=max_points)
            self._scope.set_series(name, t_all, y_all, pen)
            self._refresh_latest_value_cell(name)
            if self._slider_action.isChecked():
                self._refresh_slider_legend_values()
        else:
            self.set_channel_data(name, t_new, y_new)

    def _set_channel_highlight(self, name: str, checked: bool) -> None:
        row = self._channel_legend_row.get(name)
        if row is None:
            return
        if checked:
            self._highlighted_channels.add(name)
        else:
            self._highlighted_channels.discard(name)

        for col in range(self._legend_table.columnCount()):
            item = self._legend_table.item(row, col)
            if item is None:
                continue
            f = item.font()
            f.setBold(checked)
            item.setFont(f)

        nl = self._channel_name_labels.get(name)
        if nl is not None:
            nf = nl.font()
            nf.setBold(checked)
            nl.setFont(nf)

        self._registry.set_highlight(name, checked)
        pen = self._pen_for_channel(name)
        if pen is not None:
            pair = self._scope.get_series(name)
            if pair is not None:
                tx, ty = pair
                self._scope.set_series(name, tx, ty, pen)
            self._scope.refresh_pixmap()

    def _on_adjust(self) -> None:
        self._scope.auto_range()

    def _on_walk_toggled(self, on: bool) -> None:
        if self._walk_action is None:
            return
        self._scope.set_walking_axis(on, self._walk_span)

    def _default_save_dir(self) -> Path:
        if self._save_last_dir is not None and self._save_last_dir.is_dir():
            return self._save_last_dir
        return Path.home()

    def _next_save_filename(self) -> Path:
        ext = ".mf4" if self._save_last_format == "mdf" else ".parquet" if self._save_last_format == "parquet" else ".csv"
        stem = self._save_last_basename or "measurement"
        base_dir = self._default_save_dir()
        candidate = base_dir / f"{stem}{ext}"
        if not candidate.exists():
            return candidate
        idx = 1
        while True:
            candidate = base_dir / f"{stem}_{idx}{ext}"
            if not candidate.exists():
                return candidate
            idx += 1

    def _on_save_visible_channels(self) -> None:
        series_buffers: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for name in self._registry.names():
            pair = self._scope.get_series(name)
            if pair is None:
                continue
            tx, ty = pair
            if len(tx) == 0:
                continue
            series_buffers[name] = (tx, ty)
        if not series_buffers:
            QMessageBox.information(self, "Save recording", "No visible channels to save.")
            return

        suggested = self._next_save_filename()
        filters = "MDF files (*.mdf *.mf4 *.dat);;Parquet files (*.parquet *.pq);;CSV files (*.csv)"
        selected_filter = (
            "Parquet files (*.parquet *.pq)"
            if self._save_last_format == "parquet"
            else "CSV files (*.csv)"
            if self._save_last_format == "csv"
            else "MDF files (*.mdf *.mf4 *.dat)"
        )
        path_str, chosen_filter = QFileDialog.getSaveFileName(
            self,
            "Save recording",
            str(suggested),
            filters,
            selected_filter,
        )
        if not path_str:
            return

        out_path = Path(path_str)
        self._save_last_dir = out_path.parent
        self._save_last_basename = out_path.stem
        suf = out_path.suffix.lower()
        if "parquet" in chosen_filter or suf in (".parquet", ".pq"):
            self._save_last_format = "parquet"
        elif "csv" in chosen_filter or suf == ".csv":
            self._save_last_format = "csv"
        else:
            self._save_last_format = "mdf"

        try:
            export_recording_buffers(series_buffers, out_path, fmt=self._save_last_format)
            effective_path = out_path
            if self._save_last_format == "mdf" and not effective_path.is_file():
                alt = effective_path.with_suffix(".mf4")
                if alt.is_file():
                    effective_path = alt
            self.recording_saved.emit(str(effective_path))
        except Exception as exc:
            QMessageBox.warning(self, "Save recording", f"Could not save recording:\n{exc}")

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
    """Thin host: optional standalone layout with a single :class:`DataViewerWidget`."""

    def __init__(
        self,
        data_source: TimeSeriesDataSource | Callable[[str], tuple[np.ndarray, np.ndarray]],
        parent: QWidget | None = None,
        *,
        enable_walking_axis: bool = False,
        resolve_channel_unit: Callable[[str], str] | None = None,
        mode: PlotViewerMode | Literal["static", "dynamic"] = "static",
        legend_visible_at_start: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = DataViewerWidget(
            data_source,
            self,
            enable_walking_axis=enable_walking_axis,
            resolve_channel_unit=resolve_channel_unit,
            mode=mode,
            legend_visible_at_start=legend_visible_at_start,
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._viewer)

    @property
    def viewer(self) -> DataViewerWidget:
        return self._viewer
