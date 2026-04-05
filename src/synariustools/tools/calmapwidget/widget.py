"""Split-view widget: HoloViews (matplotlib backend) plot + matrix table (axes + heatmap cells)."""

from __future__ import annotations

import types
from pathlib import Path
from typing import cast

import matplotlib

matplotlib.use("qtagg")

import holoviews as hv
import numpy as np
from holoviews import opts
from matplotlib.backend_bases import MouseButton
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.axes3d import Axes3D
from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QResizeEvent, QShowEvent, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from synariustools.tools.calmapwidget.data import CalibrationMapData
from synariustools.tools.plotwidget.plot_theme import STUDIO_TOOLBAR_FOREGROUND, studio_toolbar_stylesheet
from synariustools.tools.plotwidget.svg_icons import icon_from_tinted_svg_file

hv.extension("matplotlib")

_ICONS_DIR = Path(__file__).resolve().parent / "icons"

_TABLE_MATRIX_COL_W = 52
# Genug für 11px-Schrift + Zellenpadding (sonst Abschneiden in den Heatmap-Zellen).
_TABLE_MATRIX_ROW_H = 26
_TABLE_FRAME_PAD = 8
# Beim Plot-Aufklappen: Mindestbreite ≈ so viele Matrix-Spalten (wie _table_matrix_viewport_px).
_PLOT_EXPAND_WIDTH_MATRIX_COLS = 6
_TABLE_VIEWPORT_MAX_CELLS = 12
_SCALAR_TABLE_MAX_W = 420

_HV_2D_CURVE_FIG_INCHES = (8.0, 4.5)
# 3D: +50 % ggü. der vorherigen (12.0, 7.3125)″ / 405 px-Höhenkappe.
_HV_3D_FIG_INCHES = (18.0, 10.96875)
_3D_SCROLL_ZOOM_STEP = 1.12
# 2D: wie matplotlib.backend_tools.ZoomPanBase.base_scale (Mausrad pro 120°-Tick).
_2D_WHEEL_BASE_SCALE = 2.0
_3D_DIST_MIN = 2.8
_3D_DIST_MAX = 48.0
_PLOT_VIEWPORT_MAX_H = 360
# Mindesthöhe Plot (Y-Verkleinerung bis hier); muss zu _sync_plot_scroll_outer_size passen.
_PLOT_VIEWPORT_MIN_H = 200
# 3D-Kennfelder: gleiche Zielhöhe wie 2D — größere HoloViews-Figur wird per _sync auf die Viewport-Pixel gemappt.
# Obere Grenze bei sehr großen Fenstern (Matplotlib-Pixelpuffer).
_PLOT_VIEWPORT_SANE_MAX_PX = 4000
# 3D-Achsen-BBox fast volle Figurenhöhe (norm. 0–1), kleine Ränder für Suptitel bzw. Achsenlabels.
_3D_AXES_FILL_Y0 = 0.055
_3D_AXES_FILL_Y1 = 0.915
_3D_SUPTITLE_Y = 0.955
# Initiale „Vergrößerung“ wie N Mausrad-Ticks hinein (_qt_apply_3d_wheel_zoom, dy>0).
_3D_INITIAL_WHEEL_ZOOM_TICKS = 2

# Shown while LMB-dragging on the 3D surface (Ctrl enables elevation / tilt along Z).
_3D_ROTATION_CTRL_HINT = "Hold Ctrl while dragging to tilt the view (elevation along Z)."

# Marken für diskrete Stützstellen (gut sichtbar auf Kurve / Fläche)
_SUPPORT_MARKER_COLOR = "orangered"
_SUPPORT_MARKER_EDGE = "white"
_SUPPORT_SCATTER_OPTS = opts.Scatter(
    s=42, color=_SUPPORT_MARKER_COLOR, edgecolor=_SUPPORT_MARKER_EDGE, linewidth=1.2
)
_SUPPORT_SCATTER3D_OPTS = opts.Scatter3D(
    s=38, color=_SUPPORT_MARKER_COLOR, edgecolor=_SUPPORT_MARKER_EDGE, linewidth=1.0
)


def _heatmap_qcolor(value: float, vmin: float, vmax: float, cmap) -> tuple[QColor, QColor]:
    """Return (background, foreground) for a numeric cell."""
    if not np.isfinite(value) or not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        bg = QColor(240, 240, 240)
        return bg, QColor(30, 30, 30)
    t = (float(value) - float(vmin)) / (float(vmax) - float(vmin))
    t = max(0.0, min(1.0, t))
    rgba = cmap(t)
    r, g, b = int(rgba[0] * 255), int(rgba[1] * 255), int(rgba[2] * 255)
    bg = QColor(r, g, b)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    fg = QColor(255, 255, 255) if lum < 140 else QColor(20, 20, 20)
    return bg, fg


def _enable_3d_mouse_rotation(fig: Figure) -> None:
    """Pan (Mitte) und Zoom (rechts) wie bei Matplotlib; Rotation über eigene Callbacks (Turntable / Strg+frei)."""
    for ax in fig.axes:
        if isinstance(ax, Axes3D):
            ax.mouse_init(rotate_btn=[], pan_btn=2, zoom_btn=3)


def _apply_synarius_3d_rotation_delta(
    ax: Axes3D, dx: float, dy: float, *, full_rotation: bool
) -> None:
    """Wie Matplotlib ``axes3d.mouserotationstyle='azel'``, aber ohne Strg nur Azimut (Turntable um Z)."""
    w = float(ax._pseudo_w)
    h = float(ax._pseudo_h)
    if w == 0.0 or h == 0.0:
        return
    roll = np.deg2rad(ax.roll)
    if full_rotation:
        delev = -(dy / h) * 180 * np.cos(roll) + (dx / w) * 180 * np.sin(roll)
        dazim = -(dy / h) * 180 * np.sin(roll) - (dx / w) * 180 * np.cos(roll)
    else:
        delev = 0.0
        dazim = -(dx / w) * 180
    vertical_axis = ax._axis_names[ax._vertical_axis]
    ax.view_init(
        elev=ax.elev + delev,
        azim=ax.azim + dazim,
        roll=ax.roll,
        vertical_axis=vertical_axis,
        share=True,
    )


def _patch_axes3d_preserve_dist(ax: Axes3D) -> None:
    """Axes3D.view_init setzt _dist fest auf 10; die Rotation ruft view_init pro Bewegung auf."""
    if getattr(ax, "_synarius_dist_preserve", False):
        return

    def view_init_preserve(
        self: Axes3D,
        elev=None,
        azim=None,
        roll=None,
        vertical_axis: str = "z",
        share: bool = False,
    ) -> None:
        saved = self._dist
        Axes3D.view_init(self, elev=elev, azim=azim, roll=roll, vertical_axis=vertical_axis, share=share)
        self._dist = max(_3D_DIST_MIN, min(_3D_DIST_MAX, float(saved)))

    ax.view_init = types.MethodType(view_init_preserve, ax)
    ax._synarius_dist_preserve = True


def _apply_3d_initial_zoom_like_wheel(ax: Axes3D, n_ticks: int) -> None:
    """Wie ``n_ticks``-mal Mausrad-Zoom hinein (``_dist`` kleiner → Szene größer)."""
    if n_ticks <= 0:
        return
    factor = _3D_SCROLL_ZOOM_STEP**n_ticks
    ax._dist = max(_3D_DIST_MIN, min(_3D_DIST_MAX, float(ax._dist) / factor))


def _relax_3d_plot_clipping(fig: Figure) -> None:
    """Ohne das clippt Matplotlib 3D an der Achsen-Bounding-Box — starkes Reinzoomen schneidet die Fläche ab."""
    for ax in fig.axes:
        if not isinstance(ax, Axes3D):
            continue
        try:
            ax.patch.set_clip_on(False)
        except Exception:
            pass
        for ch in ax.get_children():
            try:
                if hasattr(ch, "set_clip_on"):
                    ch.set_clip_on(False)
            except Exception:
                pass


def _layout_3d_colorbar_at_right_edge(fig: Figure) -> None:
    """Colorbar an den rechten Rand; 3D-Plot bleibt in der verbleibenden Fläche horizontal zentriert.

    Die Achsenposition von HoloViews wird nur für Breite/Höhe/V-Offset ausgelesen, dann der Plot in
    [margin, zone_right] zentriert — nicht zusammen mit der Legende nach links gezogen.
    """
    axes_3d = [ax for ax in fig.axes if isinstance(ax, Axes3D)]
    axes_flat = [ax for ax in fig.axes if ax not in axes_3d]
    if len(axes_3d) != 1 or len(axes_flat) != 1:
        return
    ax3, cax = axes_3d[0], axes_flat[0]
    if cax.get_position().width > 0.18:
        return

    p = ax3.get_position()
    w0, h0 = float(p.width), float(p.height)

    margin_l = 0.042
    cb_w = 0.028
    gap_plot_cb = 0.01
    label_pad = 0.038
    # Zusätzlicher Abstand vom rechten Figurenrand: Colorbar + Plot etwas nach links.
    extra_inset_right = 0.06
    strip_left = 1.0 - cb_w - label_pad - extra_inset_right
    zone_right_plot = strip_left - gap_plot_cb
    zone_w = zone_right_plot - margin_l

    w3 = float(w0)
    if w3 > zone_w * 0.995:
        w3 = zone_w * 0.995

    plot_nudge_left = 0.042
    new_x0 = margin_l + (zone_w - w3) * 0.5 - plot_nudge_left
    new_x0 = max(margin_l, min(new_x0, zone_right_plot - w3 - gap_plot_cb * 0.5))
    y0 = float(_3D_AXES_FILL_Y0)
    h3 = float(_3D_AXES_FILL_Y1) - y0
    h3 = max(0.3, h3)
    ax3.set_position([new_x0, y0, w3, h3])

    cb_left = strip_left
    cb_h = max(0.35, min(h3, h0) * 0.9)
    cb_bottom = y0 + max(0.0, (h3 - cb_h) * 0.5)
    cax.set_position([cb_left, cb_bottom, cb_w, cb_h])
    try:
        cax.tick_params(axis="y", labelsize=9, pad=2)
    except Exception:
        pass


def _apply_3d_figure_suptitle(fig: Figure, title_text: str) -> None:
    """Kennfeld-Titel als Figure-Suptitle: mplot3d setzt ``Axes3D.title`` bei jedem draw zurück, Suptitle nicht."""
    fs = matplotlib.rcParams.get("figure.titlesize", 12.0)
    fig.suptitle(title_text, y=_3D_SUPTITLE_Y, fontsize=fs)
    try:
        st = getattr(fig, "_suptitle", None)
        if st is not None:
            st.set_clip_on(False)
    except Exception:
        pass


class CalibrationMapWidget(QWidget):
    """Toolbar, darunter Tabelle; Plot optional (initial ausgeblendet).

    Bevorzugte Höhe kommt aus :meth:`sizeHint`; die Mindesthöhe (:meth:`minimumSizeHint`) bleibt niedriger,
    damit das Fenster bei sichtbarem Graphen in Y verkleinert werden kann — der Plot folgt per Resize.
    """

    def __init__(self, data: CalibrationMapData, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data = data
        try:
            self._cmap = matplotlib.colormaps["viridis"]
        except Exception:
            from matplotlib import cm

            self._cmap = cm.get_cmap("viridis")
        self._table_visible = True
        self._plot_visible = False
        self._plot_rendered = False
        # Kennfeld (2D): 3D-Oberfläche. Kennlinie (1D): 2D-Funktionsgraph.
        self._3d_interaction_active = False
        self._3d_rotation_cids: list[int] = []
        self._3d_rotation_drag: tuple[Axes3D, float, float] | None = None
        self._hv_plot: object | None = None
        self._graph_plot_active = True
        self._last_plot_block = QSize(0, 0)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)

        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setStyleSheet(studio_toolbar_stylesheet())
        icon_fg = QColor(STUDIO_TOOLBAR_FOREGROUND)

        _vals_ndim = np.asarray(data.values, dtype=np.float64).ndim
        _plot_kind_icon = "map.svg" if _vals_ndim == 2 else "curve.svg"
        self._act_plot = QAction("Plot", self)
        self._act_plot.setIcon(icon_from_tinted_svg_file(_ICONS_DIR / _plot_kind_icon, icon_fg))
        self._act_plot.setCheckable(True)
        self._act_plot.setChecked(False)
        self._act_plot.setToolTip(
            "3D-Kennfeld: Ziehen = Drehung um Z; Strg+Ziehen = volle Neigung (Elevation). "
            "Sonst: Plot ein-/ausblenden."
            if _vals_ndim == 2
            else "Show or hide the plot area"
        )
        self._act_plot.toggled.connect(self._on_plot_toggled)

        self._act_table = QAction("Table", self)
        self._act_table.setIcon(icon_from_tinted_svg_file(_ICONS_DIR / "table.svg", icon_fg))
        self._act_table.setCheckable(True)
        self._act_table.setChecked(True)
        self._act_table.setToolTip("Show or hide the data table")
        self._act_table.toggled.connect(self._on_table_toggled)

        self._act_details = QAction("Details", self)
        self._act_details.setIcon(icon_from_tinted_svg_file(_ICONS_DIR / "help-about-symbolic.svg", icon_fg))
        self._act_details.setToolTip("Parameter metadata not shown in the data table")
        self._act_details.triggered.connect(self._on_parameter_details_triggered)

        self._toolbar.addAction(self._act_table)
        self._toolbar.addAction(self._act_plot)
        self._toolbar.addAction(self._act_details)
        root.addWidget(self._toolbar, 0)

        self._table = QTableWidget(self)
        self._table.setAlternatingRowColors(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(True)
        self._table.horizontalScrollBar().rangeChanged.connect(self._on_matrix_table_horizontal_scroll_changed)

        self._plot_column = QWidget()
        plot_lay = QVBoxLayout(self._plot_column)
        plot_lay.setContentsMargins(0, 0, 0, 0)
        plot_lay.setSpacing(0)

        self._figure = Figure(figsize=(6.5, 5.5), layout="tight")
        self._plot_canvas_host = QWidget(self._plot_column)
        self._canvas = FigureCanvasQTAgg(self._figure)
        self._canvas.setParent(self._plot_canvas_host)
        # Ignored: Größe nur per resize() — sonst bleibt sizeHint/min zu groß und blockiert Verkleinern.
        self._canvas.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._3d_rotation_hint = QLabel(_3D_ROTATION_CTRL_HINT, self._plot_canvas_host)
        self._3d_rotation_hint.setWordWrap(False)
        self._3d_rotation_hint.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
        )
        self._3d_rotation_hint.setStyleSheet(
            "color: rgba(45, 45, 45, 245); font-size: 10px; "
            "background: rgba(255, 255, 255, 200); padding: 3px 7px; border-radius: 3px;"
        )
        self._3d_rotation_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._3d_rotation_hint.hide()

        self._plot_scroll = QScrollArea(self._plot_column)
        self._plot_scroll.setWidget(self._plot_canvas_host)
        self._plot_scroll.setWidgetResizable(False)
        self._plot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._plot_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._plot_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._plot_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._plot_scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # Kein Mindestmaß aus dem letzten großen Plot — sonst kann das Layout beim Verkleinern nicht schrumpfen.
        self._plot_scroll.setMinimumSize(0, 0)
        self._plot_scroll.installEventFilter(self)
        self._plot_scroll.viewport().installEventFilter(self)
        self._canvas.installEventFilter(self)
        self._plot_canvas_host.installEventFilter(self)

        self._nav = NavigationToolbar2QT(self._canvas, self._plot_column)
        plot_lay.addWidget(self._plot_scroll, 1)
        plot_lay.addWidget(self._nav, 0)
        self._nav.hide()

        self._plot_column.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.MinimumExpanding
        )

        self._hint = QLabel("", self._plot_column)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet("color: #666; padding: 24px;")
        plot_lay.addWidget(self._hint)

        # Vertikaler Stretch wird in :meth:`_apply_main_vlayout_stretch` gesetzt (Plot an → nur Plot-Spalte dehnt).
        root.addWidget(self._table, 0)
        root.addWidget(self._plot_column, 0)

        self._hint.hide()
        self._plot_column.hide()

        self._build_table()
        vals = np.asarray(self._data.values, dtype=np.float64)
        if vals.ndim == 0 or not self._graph_plot_active:
            self._draw_plot()
            self._plot_rendered = vals.ndim != 0 and self._graph_plot_active
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._apply_outer_geometry()
        QTimer.singleShot(0, self._apply_table_horizontal_scrollbar_compensation)

    def _table_h_scroll_extra_height(self) -> int:
        """Zusatzhöhe, wenn die horizontale Scrollbar der Matrix-Tabelle Platz braucht (Y-Ausdehnung des Widgets)."""
        sb = self._table.horizontalScrollBar()
        if sb.maximum() <= 0:
            return 0
        h = sb.height()
        if h < 1:
            h = sb.sizeHint().height()
        return max(h, 12)

    def _apply_table_horizontal_scrollbar_compensation(self) -> None:
        vals = np.asarray(self._data.values, dtype=np.float64)
        if vals.ndim not in (1, 2) or not self._table_visible:
            return
        self._apply_table_viewport_fixed_size()
        self._apply_outer_geometry()
        if isinstance(self.window(), QDialog):
            self._maybe_adjust_host_dialog(expand_window_to_fit_plot=True)

    def _on_matrix_table_horizontal_scroll_changed(self, *_args: object) -> None:
        QTimer.singleShot(0, self._apply_table_horizontal_scrollbar_compensation)

    def _plot_wheel_targets(self) -> tuple[QObject, ...]:
        return (
            self._plot_scroll,
            self._plot_scroll.viewport(),
            self._canvas,
            self._plot_canvas_host,
        )

    def _reposition_3d_rotation_hint(self) -> None:
        lbl = self._3d_rotation_hint
        if not lbl.isVisible():
            return
        host = self._plot_canvas_host
        m = 4
        max_w = max(120, host.width() - 2 * m)
        lbl.setMaximumWidth(max_w)
        lbl.adjustSize()
        x = m
        y = max(m, host.height() - lbl.height() - m)
        lbl.move(x, y)

    def _set_3d_rotation_hint_visible(self, visible: bool) -> None:
        if not self._3d_interaction_active:
            visible = False
        if visible:
            self._3d_rotation_hint.show()
            self._reposition_3d_rotation_hint()
        else:
            self._3d_rotation_hint.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Wheel trifft Viewport/Canvas, selten die QScrollArea — alle Ziele abdecken."""
        if watched is self._plot_canvas_host and event.type() == QEvent.Type.Resize:
            self._reposition_3d_rotation_hint()
            return False
        if watched in self._plot_wheel_targets() and event.type() == QEvent.Type.Wheel:
            wheel = cast(QWheelEvent, event)
            dy = wheel.angleDelta().y()
            if dy == 0:
                return False
            if wheel.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                hbar = self._plot_scroll.horizontalScrollBar()
                hbar.setValue(hbar.value() - dy // 12)
                return True
            if self._3d_interaction_active:
                self._qt_apply_3d_wheel_zoom(dy)
                return True
            if self._qt_apply_2d_wheel_zoom(wheel):
                return True
            return False
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._plot_visible and self._graph_plot_active and self._plot_rendered:
            # Nach dem Layout-Schritt: Viewport-Breite/-Höhe erst dann zuverlässig (auch beim Schrumpfen).
            QTimer.singleShot(0, self._sync_plot_scroll_outer_size)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # Beim ersten Show sitzt der Viewer erst im QDialog; vorher war die Shell ggf. unter dem MainWindow.
        # Zudem liefert die Toolbar ihre finale Höhe oft erst nach Polish/Layout — wie nach Plot-Toggle, wo
        # _maybe_adjust_host_dialog passt Dialog-Mindestmaße an den Viewer an.
        if getattr(self, "_calmap_did_sync_host_on_show", False):
            return
        self._calmap_did_sync_host_on_show = True
        self._apply_outer_geometry()
        self._maybe_adjust_host_dialog()

    def sizeHint(self) -> QSize:
        return self._compute_outer_size_from_children()

    def minimumSizeHint(self) -> QSize:
        return self._compute_minimum_outer_size()

    def _table_matrix_viewport_px(self) -> tuple[int, int]:
        """Sichtbare Zellen max. 12×12; größere Tabellen intern scrollbar. -> (width_px, height_px)."""
        vals = np.asarray(self._data.values, dtype=np.float64)
        cap = _TABLE_VIEWPORT_MAX_CELLS
        if vals.ndim == 2:
            nx, ny = int(vals.shape[0]), int(vals.shape[1])
            nrows, ncols = nx + 1, ny + 1
            vr = min(nrows, cap)
            vc = min(ncols, cap)
            return vc * _TABLE_MATRIX_COL_W + _TABLE_FRAME_PAD, vr * _TABLE_MATRIX_ROW_H + _TABLE_FRAME_PAD
        if vals.ndim == 1:
            n = int(vals.shape[0])
            vc = min(n + 1, cap)
            vr = 2
            return vc * _TABLE_MATRIX_COL_W + _TABLE_FRAME_PAD, vr * _TABLE_MATRIX_ROW_H + _TABLE_FRAME_PAD
        return 280, _TABLE_MATRIX_ROW_H * 2 + _TABLE_FRAME_PAD

    def _matrix_table_target_wh(self) -> tuple[int, int] | None:
        """Pixel size matching :meth:`_apply_table_viewport_fixed_size` (for geometry before first layout)."""
        vals = np.asarray(self._data.values, dtype=np.float64)
        if vals.ndim not in (1, 2):
            return None
        cap = _TABLE_VIEWPORT_MAX_CELLS
        nrows = self._table.rowCount()
        ncols = self._table.columnCount()
        cw_sum = sum(self._table.columnWidth(i) for i in range(ncols))
        rh_sum = sum(self._table.rowHeight(i) for i in range(nrows))
        # Before the first layout/polish, Qt often reports 0 widths/heights → clipped rows / wrong dialog size.
        if cw_sum < ncols * _TABLE_MATRIX_COL_W // 2:
            cw_sum = ncols * _TABLE_MATRIX_COL_W
        if rh_sum < nrows * _TABLE_MATRIX_ROW_H // 2:
            rh_sum = nrows * _TABLE_MATRIX_ROW_H
        vh = self._table.verticalHeader().width() if self._table.verticalHeader().isVisible() else 0
        hh = self._table.horizontalHeader().height() if self._table.horizontalHeader().isVisible() else 0
        intrinsic_w = cw_sum + vh + _TABLE_FRAME_PAD
        intrinsic_h = rh_sum + hh + _TABLE_FRAME_PAD
        tw_cap, th_cap = self._table_matrix_viewport_px()
        if nrows <= cap and ncols <= cap:
            return intrinsic_w, intrinsic_h
        return tw_cap, th_cap

    def _default_plot_viewport_px(self) -> tuple[int, int]:
        """Pixelgröße für Size-Hints / Mindestlayout (unabhängig vom aktuellen Fenster)."""
        m = self._matrix_table_target_wh()
        tw = max(1, self._table.width())
        if m is not None:
            tw = max(tw, m[0])
        w_six_cols = _TABLE_MATRIX_COL_W * _PLOT_EXPAND_WIDTH_MATRIX_COLS + _TABLE_FRAME_PAD
        w = max(200, tw, w_six_cols)
        return w, _PLOT_VIEWPORT_MAX_H

    def _apply_table_viewport_fixed_size(self) -> None:
        vals = np.asarray(self._data.values, dtype=np.float64)
        if vals.ndim in (1, 2):
            m = self._matrix_table_target_wh()
            if m is None:
                return
            tw, th = m
            th += self._table_h_scroll_extra_height()
            # Mindestgröße = gekappter Viewport (12×12); darüber hinaus mit dem Dialog mitwachsen.
            self._table.setMinimumSize(tw, th)
            # Mit sichtbarem Plot: Höhe deckeln — QTableWidget.sizeHint() bleibt ~192px hoch; Preferred/Stretch-0
            # würde sonst weiterhin diese „Lieblingshöhe“ bekommen (Logs: table_h=192 bei th=72).
            if self._plot_visible:
                self._table.setMaximumSize(16777215, th)
            else:
                self._table.setMaximumSize(16777215, 16777215)
            # Mit sichtbarem Plot: vertikal nur Preferred — sonst teilt sich die VBox die Höhe 1:1 (leere Fläche unter der Tabelle).
            vpol = (
                QSizePolicy.Policy.Preferred
                if self._plot_visible
                else QSizePolicy.Policy.Expanding
            )
            self._table.setSizePolicy(QSizePolicy.Policy.Expanding, vpol)
        elif vals.ndim == 0:
            self._table.setMinimumHeight(_TABLE_MATRIX_ROW_H + 8)
            self._table.setMaximumHeight(_TABLE_MATRIX_ROW_H * 3)
            self._table.setMinimumWidth(200)
            self._table.setMaximumWidth(_SCALAR_TABLE_MAX_W)
            self._table.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        else:
            self._table.setFixedHeight(_TABLE_MATRIX_ROW_H * 2 + _TABLE_FRAME_PAD)
            self._table.setMinimumWidth(200)
            self._table.setMaximumWidth(_SCALAR_TABLE_MAX_W)
            self._table.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def _sync_plot_scroll_outer_size(self) -> None:
        if not self._graph_plot_active:
            return
        fig = self._canvas.figure
        if fig is None:
            return
        w_in, h_in = fig.get_size_inches()
        dpi = float(fig.dpi)
        nat_ch = int(max(220, np.ceil(h_in * dpi)))
        is_3d = self._3d_interaction_active or (
            bool(fig.axes) and any(isinstance(a, Axes3D) for a in fig.axes)
        )
        default_cap = _PLOT_VIEWPORT_MAX_H
        if is_3d:
            nat_ch = max(nat_ch, default_cap)

        vp = self._plot_scroll.viewport()
        min_plot_h = _PLOT_VIEWPORT_MIN_H
        cw = max(200, vp.width())
        vph = vp.height()
        if cw < 220:
            m = self._matrix_table_target_wh()
            tw = max(200, self._table.width(), m[0] if m else 200)
            cw = max(220, tw, max(0, self.width() - 8))
        if vph < min_plot_h:
            view_h = int(max(min_plot_h, min(max(nat_ch, default_cap), default_cap)))
        else:
            view_h = int(max(min_plot_h, min(vph, _PLOT_VIEWPORT_SANE_MAX_PX)))

        win_new = cw / dpi
        hin_new = view_h / dpi
        try:
            fig.set_size_inches(win_new, hin_new, forward=True)
        except TypeError:
            fig.set_size_inches(win_new, hin_new)
        # Nur explizite Pixelgröße — kein setMinimumSize(cw, view_h): das blockiert Verkleinern (Layout + Canvas).
        self._plot_canvas_host.setFixedSize(cw, view_h)
        self._canvas.setMinimumSize(0, 0)
        self._canvas.setGeometry(0, 0, cw, view_h)
        self._last_plot_block = QSize(cw, view_h)
        self._reposition_3d_rotation_hint()
        self._canvas.draw()
        if fig.axes and any(isinstance(a, Axes3D) for a in fig.axes):
            _relax_3d_plot_clipping(fig)
            _layout_3d_colorbar_at_right_edge(fig)
            self._canvas.draw()
            # mplot3d setzt die Achsen-BBox beim draw() oft zurück — Layout erneut nach dem zweiten Draw.
            _layout_3d_colorbar_at_right_edge(fig)
            self._canvas.draw()
        self._plot_scroll.verticalScrollBar().setValue(0)
        self._plot_scroll.horizontalScrollBar().setValue(0)

    def _apply_outer_geometry(self) -> None:
        """Höhe/Breite = exakt Toolbar + sichtbare Kinder; Breite nicht breiter als breitestes Kind."""
        self._apply_table_viewport_fixed_size()
        if self._plot_visible and self._graph_plot_active and self._plot_rendered:
            self._sync_plot_scroll_outer_size()
        mw = 0
        if self._table_visible:
            mw = max(mw, self._table.width())
        if self._plot_visible and self._graph_plot_active and self._plot_rendered:
            pw, _ph = self._default_plot_viewport_px()
            mw = max(mw, pw)
        if mw > 0:
            self._toolbar.setMinimumWidth(max(self._toolbar.sizeHint().width(), mw))
        else:
            self._toolbar.setMinimumWidth(0)
        root_ly = self.layout()
        if root_ly is not None:
            self._apply_main_vlayout_stretch()
            root_ly.activate()
        self.setMinimumSize(self._compute_minimum_outer_size())
        self.updateGeometry()

    def _apply_main_vlayout_stretch(self) -> None:
        """Vertikal: Zusatzplatz nur in der Plot-Spalte, nicht unter der Tabelle (1:1-Stretch vermeiden)."""
        ly = self.layout()
        if not isinstance(ly, QVBoxLayout):
            return
        it = ly.indexOf(self._table)
        ip = ly.indexOf(self._plot_column)
        if it < 0 or ip < 0:
            return
        if self._plot_visible:
            ly.setStretch(it, 0)
            ly.setStretch(ip, 1)
        else:
            ly.setStretch(it, 1)
            ly.setStretch(ip, 0)

    def _deferred_sync_plot_after_expand(self) -> None:
        if self._plot_visible and self._graph_plot_active and self._plot_rendered:
            self._sync_plot_scroll_outer_size()

    def _should_shrink_dialog_to_content(self) -> bool:
        """Nach Einklappen von Plot oder Tabelle: Dialog auf :meth:`sizeHint` (nur sichtbare Teile) verkleinern."""
        if not self._plot_visible and self._graph_plot_active:
            return True
        if not self._table_visible and self._plot_visible:
            return True
        return False

    def _maybe_adjust_host_dialog(
        self,
        *,
        shrink_window_to_content: bool = False,
        # Plot aufklappen oder Tabelle wieder ein: Fenster mindestens auf :meth:`sizeHint` vergrößern.
        expand_window_to_fit_plot: bool = False,
    ) -> None:
        shell = self.parentWidget()
        if shell is not None:
            sly = shell.layout()
            if sly is not None:
                sly.activate()
            shell.updateGeometry()
            # Sonst blockiert die einmalige setMinimumSize() der Shell beim __init__ das Schmalwerden.
            s_req = shell.minimumSizeHint()
            if s_req.width() >= 1 and s_req.height() >= 1:
                shell.setMinimumSize(s_req.width(), s_req.height())
        win = self.window()
        if isinstance(win, QDialog):
            dly = win.layout()
            if dly is not None:
                dly.activate()
            req = self.minimumSizeHint()
            if req.width() >= 1 and req.height() >= 1:
                # Immer an den aktuellen Inhalt koppeln — sonst bleibt nach Einklappen des Plots eine zu große Mindesthöhe.
                win.setMinimumSize(req.width(), req.height())
            if shrink_window_to_content:
                sz = self.sizeHint()
                if sz.width() >= 1 and sz.height() >= 1:
                    # Sichtbare Bereiche: z. B. nur Tabelle, nur Plot, oder beides.
                    win.resize(sz.width(), sz.height())
            if expand_window_to_fit_plot:
                sz = self.sizeHint()
                if sz.width() >= 1 and sz.height() >= 1:
                    cur = win.size()
                    win.resize(max(cur.width(), sz.width()), max(cur.height(), sz.height()))
            win.updateGeometry()
        if expand_window_to_fit_plot:
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
            if self._plot_visible and self._graph_plot_active and self._plot_rendered:
                self._sync_plot_scroll_outer_size()
                QTimer.singleShot(0, self._deferred_sync_plot_after_expand)

    def _compute_outer_size_from_children(self) -> QSize:
        # Toolbar: Breite nur aus sizeHint — tb.width() folgt dem verbreiterten Fenster und würde sizeHint nach
        # Einklappen des Plots falsch groß halten (kein Schrumpfen per resize).
        tb = self._toolbar
        w = max(1, tb.sizeHint().width())
        h = tb.height() if tb.height() > 0 else tb.sizeHint().height()
        if self._table_visible:
            # Matrix tables: never use QTableWidget.sizeHint() for height — before the event loop
            # height()/width() can be 0; sizeHint() is ~192px tall and leaves a black gap until replot.
            m = self._matrix_table_target_wh()
            if m is not None:
                tw, th = m
                th += self._table_h_scroll_extra_height()
            else:
                tw = self._table.width() if self._table.width() > 0 else self._table.sizeHint().width()
                th = self._table.height() if self._table.height() > 0 else self._table.sizeHint().height()
            w = max(w, tw)
            h += th
        if self._plot_visible:
            if self._graph_plot_active and self._plot_rendered:
                pw, ph = self._default_plot_viewport_px()
                w = max(w, pw)
                h += ph
            else:
                w = max(w, 220)
                h += 48
        return QSize(max(1, w), max(1, h))

    def _compute_minimum_outer_size(self) -> QSize:
        """Mindestmaße ohne volle Plot-Höhe — erlaubt vertikales Schrumpfen; Plot skaliert in :meth:`_sync_plot_scroll_outer_size`."""
        tb = self._toolbar
        w = max(1, tb.sizeHint().width())
        h = tb.height() if tb.height() > 0 else tb.sizeHint().height()
        if self._table_visible:
            m = self._matrix_table_target_wh()
            if m is not None:
                tw, th = m
                th += self._table_h_scroll_extra_height()
            else:
                tw = self._table.width() if self._table.width() > 0 else self._table.sizeHint().width()
                th = self._table.height() if self._table.height() > 0 else self._table.sizeHint().height()
            w = max(w, tw)
            h += th
        if self._plot_visible:
            if self._graph_plot_active and self._plot_rendered:
                pw, _ph = self._default_plot_viewport_px()
                w = max(w, pw)
                h += _PLOT_VIEWPORT_MIN_H
            else:
                w = max(w, 220)
                h += 48
        return QSize(max(1, w), max(1, h))

    def _disconnect_3d_rotation_callbacks(self) -> None:
        fig = self._canvas.figure
        if fig is not None:
            canvas = fig.canvas
            for cid in self._3d_rotation_cids:
                try:
                    canvas.mpl_disconnect(cid)
                except Exception:
                    pass
        self._3d_rotation_cids.clear()
        self._3d_rotation_drag = None
        self._set_3d_rotation_hint_visible(False)

    def _connect_3d_rotation_callbacks(self, fig: Figure) -> None:
        self._disconnect_3d_rotation_callbacks()
        canvas = fig.canvas
        self._3d_rotation_cids = [
            canvas.mpl_connect("button_press_event", self._on_mpl_3d_rotation_press),
            canvas.mpl_connect("motion_notify_event", self._on_mpl_3d_rotation_motion),
            canvas.mpl_connect("button_release_event", self._on_mpl_3d_rotation_release),
        ]

    def _on_mpl_3d_rotation_press(self, event: object) -> None:
        btn = getattr(event, "button", None)
        if btn != MouseButton.LEFT and btn != 1:
            return
        ax = event.inaxes
        if not isinstance(ax, Axes3D):
            return
        if event.xdata is None or event.ydata is None:
            return
        self._3d_rotation_drag = (ax, float(event.xdata), float(event.ydata))
        self._set_3d_rotation_hint_visible(True)

    def _on_mpl_3d_rotation_motion(self, event: object) -> None:
        if self._3d_rotation_drag is None:
            return
        buttons = getattr(event, "buttons", frozenset())
        if not (MouseButton.LEFT in buttons or 1 in buttons):
            return
        ax, lx, ly = self._3d_rotation_drag
        if event.inaxes is not ax or event.xdata is None or event.ydata is None:
            return
        dx = float(event.xdata) - lx
        dy = float(event.ydata) - ly
        if dx == 0.0 and dy == 0.0:
            return
        mods = getattr(event, "modifiers", ())
        full = "ctrl" in mods
        _apply_synarius_3d_rotation_delta(ax, dx, dy, full_rotation=full)
        self._3d_rotation_drag = (ax, float(event.xdata), float(event.ydata))
        ax.stale = True
        event.canvas.draw_idle()

    def _on_mpl_3d_rotation_release(self, event: object) -> None:
        btn = getattr(event, "button", None)
        if btn == MouseButton.LEFT or btn == 1:
            self._3d_rotation_drag = None
            self._set_3d_rotation_hint_visible(False)

    def _qt_apply_3d_wheel_zoom(self, dy: int) -> None:
        fig = self._canvas.figure
        if fig is None:
            return
        step = 1 if dy > 0 else -1
        for ax in fig.axes:
            if not isinstance(ax, Axes3D):
                continue
            if step > 0:
                ax._dist /= _3D_SCROLL_ZOOM_STEP
            else:
                ax._dist *= _3D_SCROLL_ZOOM_STEP
            ax._dist = max(_3D_DIST_MIN, min(_3D_DIST_MAX, ax._dist))
            ax.stale = True
        _relax_3d_plot_clipping(fig)
        self._canvas.draw()

    def _canvas_wheel_display_coords(self, wheel: QWheelEvent) -> tuple[float, float] | None:
        """Display-Koordinaten (Figure-Pixel, Matplotlib-Konvention) für Mausrad am Plot."""
        c = self._canvas
        fig = c.figure
        if fig is None:
            return None
        pos = c.mapFromGlobal(wheel.globalPosition().toPoint())
        x = float(pos.x())
        y = float(fig.bbox.height / c.device_pixel_ratio - pos.y())
        return x * c.device_pixel_ratio, y * c.device_pixel_ratio

    def _qt_apply_2d_wheel_zoom(self, wheel: QWheelEvent) -> bool:
        """2D-Kennlinie: Zoomen wie im Matplotlib-Zoom-Modus (scroll_zoom / _set_view_from_bbox)."""
        if self._3d_interaction_active:
            return False
        fig = self._canvas.figure
        if fig is None:
            return False
        xy = self._canvas_wheel_display_coords(wheel)
        if xy is None:
            return False
        x, y = xy
        dy = wheel.angleDelta().y()
        if dy == 0:
            return False
        steps = dy / 120.0
        scl = float(_2D_WHEEL_BASE_SCALE**steps)
        for ax in fig.axes:
            if isinstance(ax, Axes3D):
                continue
            if not ax.get_visible() or not ax.get_navigate() or not ax.can_zoom():
                continue
            if not ax.bbox.contains(x, y):
                continue
            ax._set_view_from_bbox([x, y, scl])
            self._canvas.draw_idle()
            return True
        return False

    def _cleanup_hv_plot(self) -> None:
        self._disconnect_3d_rotation_callbacks()
        self._3d_interaction_active = False
        if self._hv_plot is None:
            return
        try:
            cleanup = getattr(self._hv_plot, "cleanup", None)
            if callable(cleanup):
                cleanup()
        except Exception:
            pass
        self._hv_plot = None

    def _vmin_vmax(self) -> tuple[float, float]:
        v = np.asarray(self._data.values, dtype=np.float64).ravel()
        v = v[np.isfinite(v)]
        if v.size == 0:
            return 0.0, 1.0
        return float(np.min(v)), float(np.max(v))

    def _build_table(self) -> None:
        d = self._data
        vals = np.asarray(d.values, dtype=np.float64)
        vmin, vmax = self._vmin_vmax()
        axis0_label = d.axis_label(0, "x")
        axis1_label = d.axis_label(1, "y")
        value_label = d.value_label()

        self._table.horizontalHeader().setVisible(True)
        self._table.verticalHeader().setVisible(True)

        if vals.ndim == 0:
            self._table.setColumnCount(2)
            self._table.setRowCount(1)
            self._table.setHorizontalHeaderLabels(["Field", "Value"])
            self._set_heatmap_item(0, 0, QTableWidgetItem(d.title), None, None)
            v0 = float(vals.item())
            itv = QTableWidgetItem(f"{v0:g}")
            self._set_heatmap_item(0, 1, itv, v0, (vmin, vmax))
            self._hint.setText("Scalar — no plot")
            self._graph_plot_active = False
            self._act_plot.setEnabled(False)
            self._canvas.hide()
            self._plot_scroll.hide()
            self._nav.hide()
            self._hint.show()
            self._apply_labeled_table_sizing()
            self._apply_outer_geometry()
            return

        if vals.ndim == 1:
            n = int(vals.shape[0])
            ax_x = d.axis_values(0)
            self._table.horizontalHeader().setVisible(False)
            self._table.verticalHeader().setVisible(False)
            self._table.setRowCount(2)
            self._table.setColumnCount(n + 1)
            self._table.setItem(0, 0, QTableWidgetItem(axis0_label))
            for j in range(n):
                xj = float(ax_x[j]) if j < len(ax_x) else float(j)
                self._table.setItem(0, j + 1, QTableWidgetItem(f"{xj:g}"))
            self._table.setItem(1, 0, QTableWidgetItem(value_label))
            for j in range(n):
                vj = float(vals[j])
                it = QTableWidgetItem(f"{vj:g}")
                self._set_heatmap_item(1, j + 1, it, vj, (vmin, vmax))
            self._graph_plot_active = True
            self._apply_matrix_table_sizing()
            self._apply_outer_geometry()
            return

        if vals.ndim == 2:
            nx, ny = int(vals.shape[0]), int(vals.shape[1])
            ax_y = d.axis_values(0)
            ax_x = d.axis_values(1)
            self._table.horizontalHeader().setVisible(False)
            self._table.verticalHeader().setVisible(False)
            self._table.setRowCount(nx + 1)
            self._table.setColumnCount(ny + 1)
            self._table.setItem(0, 0, QTableWidgetItem(f"{axis1_label} / {axis0_label}"))
            for j in range(ny):
                xj = float(ax_x[j]) if j < len(ax_x) else float(j)
                self._table.setItem(0, j + 1, QTableWidgetItem(f"{xj:g}"))
            for i in range(nx):
                yi = float(ax_y[i]) if i < len(ax_y) else float(i)
                self._table.setItem(i + 1, 0, QTableWidgetItem(f"{yi:g}"))
                for j in range(ny):
                    vij = float(vals[i, j])
                    it = QTableWidgetItem(f"{vij:g}")
                    self._set_heatmap_item(i + 1, j + 1, it, vij, (vmin, vmax))
            self._graph_plot_active = True
            self._apply_matrix_table_sizing()
            self._apply_outer_geometry()
            return

        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["Info", "Value"])
        self._table.setRowCount(1)
        self._table.setItem(0, 0, QTableWidgetItem("Shape"))
        self._table.setItem(0, 1, QTableWidgetItem(str(vals.shape)))
        self._hint.setText(f"Unsupported rank ({vals.ndim}D) for this viewer")
        self._graph_plot_active = False
        self._hint.show()
        self._canvas.hide()
        self._plot_scroll.hide()
        self._nav.hide()
        self._apply_labeled_table_sizing()
        self._apply_outer_geometry()

    def _apply_matrix_table_sizing(self) -> None:
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setWordWrap(False)
        vh = self._table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for r in range(self._table.rowCount()):
            self._table.setRowHeight(r, _TABLE_MATRIX_ROW_H)
        for c in range(self._table.columnCount()):
            self._table.setColumnWidth(c, _TABLE_MATRIX_COL_W)
        for r in range(self._table.rowCount()):
            for c in range(self._table.columnCount()):
                it = self._table.item(r, c)
                if it is not None:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _apply_labeled_table_sizing(self) -> None:
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        vh = self._table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(_TABLE_MATRIX_ROW_H)
        self._table.resizeRowsToContents()

    def _set_heatmap_item(
        self,
        r: int,
        c: int,
        item: QTableWidgetItem,
        value: float | None,
        mm: tuple[float, float] | None,
    ) -> None:
        if value is None or mm is None:
            self._table.setItem(r, c, item)
            return
        bg, fg = _heatmap_qcolor(value, mm[0], mm[1], self._cmap)
        item.setBackground(bg)
        item.setForeground(fg)
        self._table.setItem(r, c, item)

    def _build_holoviews_element(self):
        d = self._data
        vals = np.asarray(d.values, dtype=np.float64)
        title = f"{d.title} ({d.category})"
        axis0_label = d.axis_label(0, "x")
        axis1_label = d.axis_label(1, "y")
        value_label = d.value_label()

        if vals.ndim == 1:
            xs = d.axis_values(0)
            if len(xs) != len(vals):
                xs = np.arange(len(vals), dtype=np.float64)
            curve = hv.Curve((xs, vals), [axis0_label], [value_label]).opts(
                opts.Curve(
                    title=title,
                    color="steelblue",
                    linewidth=2,
                    xlabel=axis0_label,
                    ylabel=value_label,
                    show_grid=True,
                    fig_inches=_HV_2D_CURVE_FIG_INCHES,
                )
            )
            m = np.isfinite(xs) & np.isfinite(vals)
            if not m.any():
                return curve
            support = hv.Scatter((xs[m], vals[m]), [axis0_label], [value_label]).opts(_SUPPORT_SCATTER_OPTS)
            return curve * support

        if vals.ndim == 2:
            n0, n1 = int(vals.shape[0]), int(vals.shape[1])
            a0 = d.axis_values(0)
            a1 = d.axis_values(1)
            if len(a0) != n0:
                a0 = np.arange(n0, dtype=np.float64)
            if len(a1) != n1:
                a1 = np.arange(n1, dtype=np.float64)

            L, K = np.meshgrid(np.arange(n0, dtype=np.intp), np.arange(n1, dtype=np.intp), indexing="ij")
            px = a1[K].ravel()
            py = a0[L].ravel()
            z_flat = vals[L, K].ravel()
            fin = np.isfinite(px) & np.isfinite(py) & np.isfinite(z_flat)

            surf = hv.Surface((a0, a1, vals.T), kdims=[axis0_label, axis1_label], vdims=[value_label])
            surf = surf.opts(
                opts.Surface(
                    title="",
                    cmap="viridis",
                    colorbar=True,
                    projection="3d",
                    fig_inches=_HV_3D_FIG_INCHES,
                    # kdims follow storage order: first axis0 (a0), second axis1 (a1).
                    xlabel=axis0_label,
                    ylabel=axis1_label,
                    zlabel=value_label,
                    azimuth=40,
                    elevation=30,
                )
            )
            if not fin.any():
                return surf
            # Surface tuple (a0, a1, Z) maps first kdim to axis0 and second kdim to axis1.
            # Scatter3D uses the same mapping.
            x1_s = a0[L].ravel()
            x2_s = a1[K].ravel()
            support = hv.Scatter3D(
                (x1_s[fin], x2_s[fin], z_flat[fin]), kdims=[axis0_label, axis1_label, value_label]
            ).opts(_SUPPORT_SCATTER3D_OPTS)
            return surf * support

        return None

    def _draw_plot(self) -> None:
        d = self._data
        vals = np.asarray(d.values, dtype=np.float64)

        if vals.ndim == 0:
            self._canvas.draw_idle()
            return

        self._plot_rendered = False
        self._cleanup_hv_plot()
        element = self._build_holoviews_element()
        if element is None:
            self._plot_scroll.hide()
            self._canvas.draw_idle()
            return

        renderer = hv.renderer("matplotlib")
        self._hv_plot = renderer.get_plot(element)
        self._hv_plot.initialize_plot()
        fig = self._hv_plot.state
        fig.set_canvas(self._canvas)
        self._canvas.figure = fig

        self._3d_interaction_active = False
        if vals.ndim == 2:
            self._3d_interaction_active = True
            _enable_3d_mouse_rotation(fig)
            self._connect_3d_rotation_callbacks(fig)
            for ax in fig.axes:
                if isinstance(ax, Axes3D):
                    _patch_axes3d_preserve_dist(ax)
                    _apply_3d_initial_zoom_like_wheel(ax, _3D_INITIAL_WHEEL_ZOOM_TICKS)
                if hasattr(ax, "set_navigate_mode"):
                    ax.set_navigate_mode(None)
            self._canvas.draw()
            _relax_3d_plot_clipping(fig)
            _layout_3d_colorbar_at_right_edge(fig)
            self._canvas.draw()
            _layout_3d_colorbar_at_right_edge(fig)
            self._canvas.draw()
            _apply_3d_figure_suptitle(fig, f"{d.title} ({d.category})")
        else:
            self._canvas.draw_idle()
        if self._plot_visible:
            self._plot_scroll.show()
            self._sync_plot_scroll_outer_size()
        else:
            self._plot_scroll.hide()
        self._plot_rendered = True
        self._apply_outer_geometry()
        self._maybe_adjust_host_dialog(
            expand_window_to_fit_plot=self._plot_visible and self._graph_plot_active,
        )

    def _on_parameter_details_triggered(self) -> None:
        dlg = QDialog(self.window())
        dlg.setWindowTitle("Parameter-Details")
        dlg.setModal(True)
        root = QVBoxLayout(dlg)
        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)
        em_dash = "\u2014"
        for label, raw in self._data.detail_rows:
            text = raw.strip() if isinstance(raw, str) else str(raw)
            if not text:
                text = em_dash
            val_lbl = QLabel(text)
            val_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val_lbl.setWordWrap(True)
            form.addRow(f"{label}:", val_lbl)
        scroll = QScrollArea(dlg)
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_host)
        scroll.setMinimumSize(400, 240)
        root.addWidget(scroll)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(dlg.accept)
        root.addWidget(btns)
        dlg.resize(480, 360)
        dlg.exec()

    def _on_plot_toggled(self, checked: bool) -> None:
        if not checked and not self._act_table.isChecked():
            self._act_plot.blockSignals(True)
            self._act_plot.setChecked(True)
            self._act_plot.blockSignals(False)
            return
        self._plot_visible = bool(checked)
        self._plot_column.setVisible(self._plot_visible)
        if self._plot_visible and self._graph_plot_active and (
            not self._plot_rendered or self._canvas.figure is None
        ):
            self._draw_plot()
        elif self._plot_visible and self._graph_plot_active:
            self._plot_scroll.show()
            self._sync_plot_scroll_outer_size()
        if not self._plot_visible:
            self._plot_scroll.hide()
        self._apply_outer_geometry()
        self._maybe_adjust_host_dialog(
            shrink_window_to_content=self._should_shrink_dialog_to_content(),
            expand_window_to_fit_plot=(
                self._plot_visible and self._graph_plot_active and self._plot_rendered
            ),
        )

    def _on_table_toggled(self, checked: bool) -> None:
        if not checked and not self._act_plot.isChecked():
            self._act_table.blockSignals(True)
            self._act_table.setChecked(True)
            self._act_table.blockSignals(False)
            return
        self._table_visible = bool(checked)
        self._table.setVisible(self._table_visible)
        self._apply_outer_geometry()
        self._maybe_adjust_host_dialog(
            shrink_window_to_content=self._should_shrink_dialog_to_content(),
            expand_window_to_fit_plot=bool(checked),
        )


class CalibrationMapShell(QWidget):
    """Thin host embedding :class:`CalibrationMapWidget` (same pattern as :class:`DataViewerShell`)."""

    def __init__(self, data: CalibrationMapData, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._viewer = CalibrationMapWidget(data, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)
        lay.addWidget(self._viewer)
        min_sz = self._viewer.minimumSizeHint()
        if min_sz.width() >= 1 and min_sz.height() >= 1:
            self.setMinimumSize(min_sz)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        # Do not use _viewer.size(): before show/layout it can be the default (e.g. 640×480), so the
        # ParaWiz dialog gets the wrong height and the table clips until a later geometry refresh.
        return self._viewer.sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self._viewer.minimumSizeHint()

    @property
    def viewer(self) -> CalibrationMapWidget:
        return self._viewer
