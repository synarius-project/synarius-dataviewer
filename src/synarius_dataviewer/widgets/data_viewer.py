"""Multi-channel time-series plot: PyLinX-inspired black scope + pyqtgraph (real-time friendly)."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QMimeData, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
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
from synarius_dataviewer.widgets.channel_sidebar import MIME_CHANNEL

pg.setConfigOptions(foreground="w", background="k")

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

    Scope rendering uses pyqtgraph (scene graph). PyLinX_alt uses a QWidget + QPixmap + QPainter
    scope instead — fewer scene items, often better for very high-rate redraws; migrating would
    be a larger change.

    Real-time: call :meth:`set_channel_data` or :meth:`append_samples` from any thread only via Qt
    signals — for in-process Studio integration, call from the GUI thread or use
    ``QMetaObject.invokeMethod`` / a queued signal.
    """

    channel_drop_requested = Signal(str)
    _color_index: int

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
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._walking = False
        self._walk_span = 10.0
        self._legend_visible = True
        self._channel_legend_row: dict[str, int] = {}
        self._legend_split_saved = 380
        self._line_a: pg.InfiniteLine | None = None
        self._line_b: pg.InfiniteLine | None = None
        self._slider_legend_timer = QTimer(self)
        self._slider_legend_timer.setSingleShot(True)
        self._slider_legend_timer.setInterval(40)
        self._slider_legend_timer.timeout.connect(self._refresh_slider_legend_values)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)

        plot_column = QWidget()
        plot_column_lay = QVBoxLayout(plot_column)
        plot_column_lay.setContentsMargins(0, 0, 0, 0)
        plot_column_lay.setSpacing(0)

        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setStyleSheet(theme.studio_toolbar_stylesheet())

        act_adjust = self._toolbar.addAction("Adjust")
        act_adjust.setToolTip("Autoscale X/Y (PyLinX-style Ctrl+A)")
        act_adjust.triggered.connect(self._on_adjust)

        self._walk_action = None
        if enable_walking_axis:
            self._walk_action = self._toolbar.addAction("Walking axis")
            self._walk_action.setCheckable(True)
            self._walk_action.setToolTip("Keep a rolling time window on the X axis")
            self._walk_action.toggled.connect(self._on_walk_toggled)

        self._legend_action = self._toolbar.addAction("Legend")
        self._legend_action.setCheckable(True)
        self._legend_action.setChecked(True)
        self._legend_action.setToolTip("Show or hide the signal list (adjusts window width)")
        self._legend_action.toggled.connect(self._on_legend_panel_toggled)

        self._slider_action = self._toolbar.addAction("Slider")
        self._slider_action.setCheckable(True)
        self._slider_action.setToolTip("Show two vertical cursors (A/B); values appear in the legend columns")
        self._slider_action.toggled.connect(self._on_slider_toggled)

        act_clear = self._toolbar.addAction("Clear")
        act_clear.triggered.connect(self.clear_channels)

        plot_column_lay.addWidget(self._toolbar)

        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "time", units="s")
        self._plot.showGrid(x=True, y=True, alpha=0.35)
        self._plot.setBackground("#000000")
        # PyLinX: drawRect around the data rect after grid — here ViewBox border closes X/Y into one rectangle.
        _scope_pen = pg.mkPen("#ffffff", width=1)
        vb = self._plot.getViewBox()
        vb.setBorder(_scope_pen)
        vb.setDefaultPadding(0.02)
        for axis_name in ("left", "bottom"):
            ax = self._plot.getAxis(axis_name)
            ax.setPen(_scope_pen)
            ax.setTextPen("#e0e0e0")

        plot_column_lay.addWidget(self._plot, 1)

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
        self._legend_table.setAlternatingRowColors(True)
        self._legend_table.verticalHeader().setVisible(False)
        self._legend_table.setShowGrid(True)
        self._legend_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._legend_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hdr = self._legend_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._legend_table.verticalHeader().setDefaultSectionSize(18)
        legend_lay.addWidget(self._legend_table)

        self._splitter.addWidget(plot_column)
        self._splitter.addWidget(self._legend_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([900, 400])

        layout.addWidget(self._splitter, 1)

        self.setAcceptDrops(True)
        self._plot.setAcceptDrops(True)
        self._plot.installEventFilter(self)
        vp = self._plot.viewport()
        if vp is not None:
            vp.setAcceptDrops(True)
            vp.installEventFilter(self)

        self._empty_hint = QLabel(
            "Drag channel names here or use Plot selected in the sidebar.", self._plot
        )
        self._empty_hint.setStyleSheet("color: #888; background: transparent;")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._position_hint()

    def _position_hint(self) -> None:
        if self._empty_hint.isVisible():
            vp = self._plot.viewport()
            if vp is not None:
                r = vp.rect()
                self._empty_hint.setGeometry(r.adjusted(20, 20, -20, -20))

    def _next_pen(self) -> QPen:
        color = _COLOR_CYCLE[self._color_index % len(_COLOR_CYCLE)]
        self._color_index += 1
        return pg.mkPen(color, width=1.5)

    def _on_legend_panel_toggled(self, checked: bool) -> None:
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

    def _on_slider_toggled(self, on: bool) -> None:
        if on:
            self._ensure_sliders()
            self._refresh_slider_legend_values()
            return
        self._destroy_sliders()
        self._clear_slider_legend_cells()

    def _ensure_sliders(self) -> None:
        if self._line_a is not None and self._line_b is not None:
            return
        xa, xb = self._default_slider_x_positions()
        # Thin visible pens; wide hoverPen widens InfiniteLine hit-testing (pyqtgraph boundingRect).
        pen_a = pg.mkPen("#ffcc00", width=2)
        pen_b = pg.mkPen("#ff66ff", width=2)
        hover_a = pg.mkPen("#ffff99", width=18)
        hover_b = pg.mkPen("#ffaaff", width=18)
        self._line_a = pg.InfiniteLine(
            xa,
            angle=90,
            movable=True,
            pen=pen_a,
            hoverPen=hover_a,
        )
        self._line_b = pg.InfiniteLine(
            xb,
            angle=90,
            movable=True,
            pen=pen_b,
            hoverPen=hover_b,
        )
        # Above curves; omit InfLineLabel (child items can steal mouse).
        self._line_a.setZValue(1_000_000)
        self._line_b.setZValue(1_000_001)
        self._line_a.sigPositionChanged.connect(self._schedule_slider_legend_refresh)
        self._line_b.sigPositionChanged.connect(self._schedule_slider_legend_refresh)
        self._line_a.sigPositionChangeFinished.connect(self._refresh_slider_legend_values)
        self._line_b.sigPositionChangeFinished.connect(self._refresh_slider_legend_values)
        self._plot.addItem(self._line_a)
        self._plot.addItem(self._line_b)

    def _destroy_sliders(self) -> None:
        self._slider_legend_timer.stop()
        for line in (self._line_a, self._line_b):
            if line is None:
                continue
            try:
                line.sigPositionChanged.disconnect()
            except (TypeError, RuntimeError):
                pass
            try:
                line.sigPositionChangeFinished.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._plot.removeItem(line)
        self._line_a = None
        self._line_b = None

    def _default_slider_x_positions(self) -> tuple[float, float]:
        if self._curves:
            mins: list[float] = []
            maxs: list[float] = []
            for n in self._curves:
                xd = self._curves[n].xData
                if xd is not None and len(xd) > 0:
                    mins.append(float(xd[0]))
                    maxs.append(float(xd[-1]))
            if mins and maxs:
                xmin, xmax = min(mins), max(maxs)
                if xmax > xmin:
                    span = xmax - xmin
                    return xmin + 0.35 * span, xmin + 0.65 * span
        xr, _yr = self._plot.viewRange()
        lo, hi = float(xr[0]), float(xr[1])
        if hi > lo:
            span = hi - lo
            return lo + 0.35 * span, lo + 0.65 * span
        return 0.0, 1.0

    def _schedule_slider_legend_refresh(self, *_args: object) -> None:
        """Debounce legend refresh during drag (avoids QTableWidget churn each move)."""
        if self._slider_action.isChecked():
            self._slider_legend_timer.start()

    def _slider_x_positions(self) -> tuple[float | None, float | None]:
        if self._line_a is None or self._line_b is None:
            return None, None
        try:
            return float(self._line_a.value()), float(self._line_b.value())
        except Exception:
            return None, None

    @staticmethod
    def _fmt_measure(v: float | None) -> str:
        if v is None or not np.isfinite(v):
            return "—"
        return f"{v:.6g}"

    def _interp_channel_at(self, name: str, xq: float) -> float | None:
        it = self._curves.get(name)
        if it is None:
            return None
        tx, ty = it.getData()
        if tx is None or ty is None or len(tx) == 0:
            return None
        txa = np.asarray(tx, dtype=np.float64).ravel()
        tya = np.asarray(ty, dtype=np.float64).ravel()
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
        if not self._slider_action.isChecked() or self._line_a is None or self._line_b is None:
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

    def _register_legend_row(self, name: str, pen: object) -> None:
        if name in self._channel_legend_row:
            return
        row = self._legend_table.rowCount()
        self._legend_table.insertRow(row)
        if isinstance(pen, QPen):
            c = pen.color()
        else:
            c = QColor(_COLOR_CYCLE[0])
        sw = QTableWidgetItem()
        sw.setFlags(Qt.ItemFlag.ItemIsEnabled)
        sw.setBackground(QBrush(c))
        sw.setToolTip(c.name(QColor.NameFormat.HexRgb))
        self._legend_table.setItem(row, 0, sw)
        nm = QTableWidgetItem(name)
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

    def add_channel(self, name: str) -> None:
        if name in self._curves:
            return
        tx, ty = self._resolve_series(name)
        if len(tx) == 0:
            return
        pen = self._next_pen()
        item = self._plot.plot(tx, ty, pen=pen, name=name)
        self._curves[name] = item
        self._register_legend_row(name, pen)
        self._empty_hint.hide()
        self._refresh_walk()

    def remove_channel(self, name: str) -> None:
        item = self._curves.pop(name, None)
        if item is None:
            return
        self._plot.removeItem(item)
        self._remove_legend_row(name)
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()
        if not self._curves:
            self._empty_hint.show()
            self._position_hint()

    def clear_channels(self) -> None:
        for item in list(self._curves.values()):
            self._plot.removeItem(item)
        self._curves.clear()
        self._color_index = 0
        self._clear_legend_rows()
        self._empty_hint.show()
        self._position_hint()
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()

    def set_channel_data(self, name: str, t: np.ndarray, y: np.ndarray) -> None:
        """Replace curve arrays (full redraw; preferred for simulation ticks)."""
        t = np.asarray(t, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if name in self._curves:
            self._curves[name].setData(t, y)
        else:
            pen = self._next_pen()
            item = self._plot.plot(t, y, pen=pen, name=name)
            self._curves[name] = item
            self._register_legend_row(name, pen)
            self._empty_hint.hide()
        if self._slider_action.isChecked():
            self._refresh_slider_legend_values()
        self._refresh_walk()

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
        if name in self._curves:
            it = self._curves[name]
            tx, ty = it.getData()
            if tx is None or len(tx) == 0:
                t_all, y_all = t_new, y_new
            else:
                t_all = np.concatenate([tx, t_new])
                y_all = np.concatenate([ty, y_new])
            if len(t_all) > max_points:
                cut = len(t_all) - max_points
                t_all = t_all[cut:]
                y_all = y_all[cut:]
            it.setData(t_all, y_all)
            if self._slider_action.isChecked():
                self._refresh_slider_legend_values()
            self._refresh_walk()
        else:
            self.set_channel_data(name, t_new, y_new)

    def _on_adjust(self) -> None:
        self._plot.autoRange()

    def _on_walk_toggled(self, on: bool) -> None:
        self._walking = bool(on)
        self._refresh_walk()

    def _refresh_walk(self) -> None:
        if self._walk_action is None or not self._walking or not self._curves:
            return
        xmax = max(float(self._curves[n].xData[-1]) for n in self._curves if self._curves[n].xData is not None)
        self._plot.setXRange(xmax - self._walk_span, xmax, padding=0.02)

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
        targets = {self._plot}
        vp = self._plot.viewport()
        if vp is not None:
            targets.add(vp)
        if watched not in targets:
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
