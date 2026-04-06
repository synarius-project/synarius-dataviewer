"""Split-view widget: HoloViews (matplotlib backend) plot + matrix table (axes + heatmap cells)."""

from __future__ import annotations

import json
import types
import time
from collections.abc import Callable
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
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCloseEvent,
    QFont,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from synariustools.tools.calmapwidget.data import CalibrationMapData
from synariustools.tools.calmapwidget.edit_table import (
    EditableCalmapTable,
    adjust_digit_in_numeric_string,
    digit_index_at_cell_pos,
)
from synariustools.tools.plotwidget.plot_theme import (
    STUDIO_TOOLBAR_FOREGROUND,
    studio_commit_toolbutton_widget_stylesheet,
    studio_toolbar_stylesheet,
)
from synariustools.tools.plotwidget.svg_icons import icon_from_tinted_svg_file

hv.extension("matplotlib")
try:
    # Irreguläre Achsen-Stützstellen: Surface-Warnung „not evenly sampled“ unterdrücken (pro Prozess).
    hv.config.image_rtol = 0.05
except Exception:
    pass

_ICONS_DIR = Path(__file__).resolve().parent / "icons"


def _host_window_icon(widget: QWidget | None) -> QIcon:
    """QApplication-Icon (ParaWiz setzt es in ``__main__``), sonst Top-Level-Fenster."""
    app = QApplication.instance()
    if app is not None:
        ico = app.windowIcon()
        if not ico.isNull():
            return ico
    if widget is not None:
        win = widget.window()
        if win is not None:
            ico = win.windowIcon()
            if not ico.isNull():
                return ico
    return QIcon()


def _parse_user_float_text(text: str) -> float | None:
    t = text.strip().replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def build_calibration_metadata_scroll_area(
    parent: QWidget, data: CalibrationMapData, *, min_h: int = 180
) -> QScrollArea:
    """Form with ``detail_rows`` inside a resizable scroll area (Details / scalar editor)."""
    form_host = QWidget(parent)
    form = QFormLayout(form_host)
    form.setContentsMargins(8, 8, 8, 8)
    form.setSpacing(8)
    em_dash = "\u2014"
    for label, raw in data.detail_rows:
        text = raw.strip() if isinstance(raw, str) else str(raw)
        if not text:
            text = em_dash
        val_lbl = QLabel(text)
        val_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        val_lbl.setWordWrap(True)
        form.addRow(f"{label}:", val_lbl)
    scroll = QScrollArea(parent)
    scroll.setWidgetResizable(True)
    scroll.setWidget(form_host)
    scroll.setMinimumSize(400, min_h)
    return scroll


def exec_scalar_calibration_edit_dialog(
    parent: QWidget | None,
    data: CalibrationMapData,
    *,
    window_icon: QIcon | None = None,
) -> float | None:
    """
    Single modal dialog: Edit-style value block + metadata (no CalibrationMap shell).

    Returns the new scalar on OK, or None if cancelled / invalid state.
    """
    vals0 = np.asarray(data.values, dtype=np.float64)
    if vals0.ndim != 0:
        return None
    dlg = QDialog(parent)
    if window_icon is not None and not window_icon.isNull():
        dlg.setWindowIcon(window_icon)
    elif parent is not None:
        _ico = _host_window_icon(parent)
        if not _ico.isNull():
            dlg.setWindowIcon(_ico)
    dlg.setWindowTitle(f"Edit — {data.title}")
    dlg.setModal(True)
    root = QVBoxLayout(dlg)
    root.setContentsMargins(12, 12, 12, 12)
    root.setSpacing(10)
    form = QFormLayout()
    form.addRow("Parameter:", QLabel(data.title))
    vlabel = data.value_label()
    if (vlabel or "").strip():
        form.addRow("Field:", QLabel(vlabel))
    if (data.unit or "").strip():
        form.addRow("Unit:", QLabel(data.unit))
    cur = f"{float(vals0.item()):g}"
    edit = QLineEdit(cur)
    edit.setClearButtonEnabled(True)
    form.addRow("Value:", edit)
    root.addLayout(form)
    sep = QFrame(dlg)
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFrameShadow(QFrame.Shadow.Sunken)
    root.addWidget(sep)
    meta_heading = QLabel("Metadata")
    _mh_font = QFont()
    _mh_font.setBold(True)
    meta_heading.setFont(_mh_font)
    root.addWidget(meta_heading)
    root.addWidget(build_calibration_metadata_scroll_area(dlg, data, min_h=200), 1)
    btns = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    root.addWidget(btns)
    committed: list[float] = []

    def try_commit() -> None:
        v = _parse_user_float_text(edit.text())
        if v is None:
            QMessageBox.warning(dlg, dlg.windowTitle(), "Invalid number.")
            return
        committed.append(float(v))
        dlg.accept()

    btns.accepted.connect(try_commit)
    btns.rejected.connect(dlg.reject)
    ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
    if ok_btn is not None:
        ok_btn.setDefault(True)
    edit.returnPressed.connect(try_commit)
    QTimer.singleShot(0, edit.setFocus)
    QTimer.singleShot(0, edit.selectAll)
    dlg.resize(max(440, dlg.sizeHint().width()), max(420, dlg.sizeHint().height()))
    if dlg.exec() != QDialog.DialogCode.Accepted or not committed:
        return None
    return committed[0]


# Auswahl: Hintergrund aufhellen + sichtbarer Rand (Heatmap bleibt lesbar).
_SELECTION_BORDER = QColor(45, 110, 210)
_SELECTION_LIGHTEN = 148


class _CalmapTableSelectionDelegate(QStyledItemDelegate):
    """Markierte Zellen: deutlich hellerer Grundton + blauer Rahmen."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if not (opt.state & QStyle.StateFlag.State_Selected):
            super().paint(painter, option, index)
            return

        bg_brush = index.data(Qt.ItemDataRole.BackgroundRole)
        fill = QColor()
        if isinstance(bg_brush, QBrush) and bg_brush.style() != Qt.BrushStyle.NoBrush:
            cc = bg_brush.color()
            if cc.isValid():
                fill = cc.lighter(_SELECTION_LIGHTEN)
        if not fill.isValid():
            fill = opt.palette.color(QPalette.ColorRole.Base).lighter(112)

        painter.save()
        painter.fillRect(opt.rect, fill)
        painter.restore()

        opt2 = QStyleOptionViewItem(opt)
        opt2.state &= ~QStyle.StateFlag.State_Selected
        opt2.backgroundBrush = QBrush(fill)
        super().paint(painter, opt2, index)

        painter.save()
        painter.setPen(QPen(_SELECTION_BORDER, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(opt.rect.adjusted(1, 1, -2, -2))
        painter.restore()

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

# Kennlinie (1D): Kurve dunkelorange; Stützstellen-Marker weiterhin steelblue (s. _SUPPORT_*).
# Hinweis: HoloViews Scatter3D nutzt standard c=Cycle() — nur ``c=`` setzt die Markerfarbe zuverlässig.
_1D_CURVE_COLOR = "#c75100"
_SUPPORT_NEW_MARKER_COLOR = "steelblue"
_SUPPORT_NEW_MARKER_EDGE = "white"
_SUPPORT_SCATTER_OPTS = opts.Scatter(
    s=42,
    color=_SUPPORT_NEW_MARKER_COLOR,
    edgecolors=_SUPPORT_NEW_MARKER_EDGE,
    linewidth=1.2,
)
_SUPPORT_SCATTER3D_OPTS = opts.Scatter3D(
    s=38,
    c=_SUPPORT_NEW_MARKER_COLOR,
    edgecolors=_SUPPORT_NEW_MARKER_EDGE,
    linewidth=1.0,
)

_DIRTY_FOREGROUND = QColor(0, 70, 190)
# #region agent log
_AGENT_DEBUG_LOG = Path(__file__).resolve().parents[5] / "debug-85e82c.log"


def _calmap_agent_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "85e82c",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(_AGENT_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


# #endregion
_BASELINE_CURVE_COLOR = "#9a9a9a"
_BASELINE_SCATTER_COLOR = "#5c5c5c"
_BASELINE_SCATTER_EDGE = "#d8d8d8"
_BASELINE_SURFACE_CMAP = "gray"


def _float_cell_dirty(a: float, b: float) -> bool:
    if not np.isfinite(a) and not np.isfinite(b):
        return False
    if np.isfinite(a) != np.isfinite(b):
        return True
    return not np.isclose(a, b, rtol=0.0, atol=0.0, equal_nan=True)


def _heatmap_qcolor(value: float, vmin: float, vmax: float, cmap) -> tuple[QColor, QColor]:
    """Return (background, foreground) for a numeric cell."""
    if not np.isfinite(value):
        bg = QColor(240, 240, 240)
        return bg, QColor(30, 30, 30)
    if not np.isfinite(vmin) or not np.isfinite(vmax):
        bg = QColor(240, 240, 240)
        return bg, QColor(30, 30, 30)
    if vmax < vmin:
        bg = QColor(240, 240, 240)
        return bg, QColor(30, 30, 30)
    # Konstante Fläche: Spanne 0 → bisher hellgrau; stattdessen Palettenmitte (t = 0.5).
    if np.isclose(vmin, vmax, rtol=0.0, atol=0.0, equal_nan=True):
        t = 0.5
    else:
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


def _mpl_or_qt_ctrl_down(event: object) -> bool:
    """Strg für 3D-Rotation: Matplotlib-``modifiers`` ist am Qt-Backend oft leer — Qt-Keyboard-State nutzen."""
    mods = getattr(event, "modifiers", ()) or ()
    if "ctrl" in mods or "control" in mods:
        return True
    app = QApplication.instance()
    if app is not None:
        return bool(app.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
    return False


def _synarius_3d_full_rotation_requested(event: object, *, turntable_lock: bool) -> bool:
    """Argument ``full_rotation`` für :func:`_apply_synarius_3d_rotation_delta`.

    ``turntable_lock`` True: wie der Kennfeld-Bearbeitungsplot — ohne Strg nur Azimut (Turntable), mit Strg volle Neigung.
    False: immer freie 3D-Rotation (optional, z. B. Kenngrößen-Vergleich).
    """
    if turntable_lock:
        return _mpl_or_qt_ctrl_down(event)
    return True


def _pad_1d_curve_axes_for_markers(
    fig: Figure, *, pad_x: float = 0.07, pad_y: float = 0.055
) -> None:
    """Kennlinie (2D): Limits etwas erweitern, damit Stützstellen-Scatter (Punktradius in px) nicht am Rand clippt."""
    for ax in fig.axes:
        if isinstance(ax, Axes3D):
            continue
        try:
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
        except Exception:
            continue
        lims = np.array([x0, x1, y0, y1], dtype=np.float64)
        if not np.isfinite(lims).all():
            continue
        xspan = float(x1 - x0)
        yspan = float(y1 - y0)
        if xspan <= 0.0:
            xspan = max(abs(x0), 1.0) * 0.02
        if yspan <= 0.0:
            yspan = max(abs(y0), 1.0) * 0.02
        px = xspan * pad_x
        py = yspan * pad_y
        try:
            ax.set_xlim(x0 - px, x1 + px)
            ax.set_ylim(y0 - py, y1 + py)
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

    def __init__(
        self,
        data: CalibrationMapData,
        parent: QWidget | None = None,
        *,
        on_applied_to_model: Callable[[CalibrationMapWidget], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._data = data
        self._on_applied_to_model = on_applied_to_model
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

        vals0 = np.asarray(data.values, dtype=np.float64)
        self._baseline_values = np.array(vals0, copy=True, dtype=np.float64)
        self._draft_values = np.array(vals0, copy=True, dtype=np.float64)
        self._baseline_axes: dict[int, np.ndarray] = {}
        self._draft_axes: dict[int, np.ndarray] = {}
        for k in range(int(vals0.ndim)):
            av = np.asarray(data.axis_values(k), dtype=np.float64).reshape(-1).copy()
            self._baseline_axes[k] = av
            self._draft_axes[k] = av.copy()
        self._pending_edits_visible = False

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
        self._act_details.setToolTip(
            "Parameter metadata not shown in the data table"
            if _vals_ndim != 0
            else "Scalar value (editable) and full parameter metadata — same as double-click on the value cell"
        )
        self._act_details.triggered.connect(self._on_parameter_details_triggered)

        self._toolbar.addAction(self._act_table)
        self._toolbar.addAction(self._act_plot)
        self._toolbar.addAction(self._act_details)
        self._tb_spacer = QWidget()
        self._tb_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(self._tb_spacer)
        self._wa_apply = self._make_commit_toolbar_button(
            "Apply", self._on_apply_edits, "SynariusToolbarCommitApply", "Commit edited values to the working copy (session)"
        )
        self._wa_discard = self._make_commit_toolbar_button(
            "Discard", self._on_discard_edits, "SynariusToolbarCommitDiscard", "Revert all edits to the last applied state"
        )
        self._wa_apply.setVisible(False)
        self._wa_discard.setVisible(False)
        self._toolbar.addAction(self._wa_apply)
        self._toolbar.addAction(self._wa_discard)
        root.addWidget(self._toolbar, 0)

        self._table = EditableCalmapTable(self, self)
        self._table.setItemDelegate(_CalmapTableSelectionDelegate(self._table))
        self._table.setAlternatingRowColors(False)
        self._table.setEditTriggers(EditableCalmapTable.EditTrigger.NoEditTriggers)
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
        vals = np.asarray(self._draft_values, dtype=np.float64)
        if vals.ndim == 0 or not self._graph_plot_active:
            self._draw_plot()
            self._plot_rendered = vals.ndim != 0 and self._graph_plot_active
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._apply_outer_geometry()
        QTimer.singleShot(0, self._apply_table_horizontal_scrollbar_compensation)

    def _make_commit_toolbar_button(
        self, text: str, slot: object, object_name: str, tooltip: str
    ) -> QWidgetAction:
        wa = QWidgetAction(self._toolbar)
        btn = QToolButton(self._toolbar)
        btn.setText(text)
        btn.setObjectName(object_name)
        btn.setToolTip(tooltip)
        btn.setAutoRaise(False)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        btn.setStyleSheet(studio_commit_toolbutton_widget_stylesheet())
        btn.clicked.connect(slot)
        wa.setDefaultWidget(btn)
        # #region agent log
        try:
            _p = Path(__file__).resolve().parents[5] / "debug-85e82c.log"
            with _p.open("a", encoding="utf-8") as _f:
                _f.write(
                    json.dumps(
                        {
                            "sessionId": "85e82c",
                            "hypothesisId": "H1",
                            "location": "calmapwidget/widget.py:_make_commit_toolbar_button",
                            "message": "commit_btn_stylesheet",
                            "data": {
                                "objectName": object_name,
                                "ss_len": len(btn.styleSheet() or ""),
                                "wa_styled_bg": bool(
                                    btn.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)
                                ),
                            },
                            "timestamp": int(time.time() * 1000),
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass
        # #endregion
        return wa

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
        vals = np.asarray(self._draft_values, dtype=np.float64)
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
        vals = np.asarray(self._draft_values, dtype=np.float64)
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
        vals = np.asarray(self._draft_values, dtype=np.float64)
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
        vals = np.asarray(self._draft_values, dtype=np.float64)
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
        full = _synarius_3d_full_rotation_requested(event, turntable_lock=True)
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

    def _has_pending_edits(self) -> bool:
        if not np.array_equal(self._draft_values.shape, self._baseline_values.shape):
            return True
        dv = np.asarray(self._draft_values, dtype=np.float64)
        bv = np.asarray(self._baseline_values, dtype=np.float64)
        if not np.allclose(dv, bv, rtol=0.0, atol=0.0, equal_nan=True):
            return True
        for k, a in self._draft_axes.items():
            b = self._baseline_axes.get(k)
            if b is None or len(a) != len(b):
                return True
            if not np.allclose(a, b, rtol=0.0, atol=0.0, equal_nan=True):
                return True
        return False

    def _update_pending_toolbar(self) -> None:
        show = self._has_pending_edits()
        if show == self._pending_edits_visible:
            return
        self._pending_edits_visible = show
        self._wa_apply.setVisible(show)
        self._wa_discard.setVisible(show)
        self._apply_outer_geometry()
        # #region agent log
        if show:
            try:
                _ba = self._wa_apply.defaultWidget()
                _bd = self._wa_discard.defaultWidget()
                _p = Path(__file__).resolve().parents[5] / "debug-85e82c.log"
                _snap = {}
                for _w in (_ba, _bd):
                    if isinstance(_w, QToolButton):
                        _snap[_w.objectName() or "?"] = {
                            "checkable": _w.isCheckable(),
                            "checked": _w.isChecked(),
                            "underMouse": _w.underMouse(),
                            "ss_len": len(_w.styleSheet() or ""),
                        }
                with _p.open("a", encoding="utf-8") as _f:
                    _f.write(
                        json.dumps(
                            {
                                "sessionId": "85e82c",
                                "hypothesisId": "H2",
                                "location": "calmapwidget/widget.py:_update_pending_toolbar",
                                "message": "commit_btn_visible_state",
                                "data": _snap,
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                        + "\n"
                    )
            except Exception:
                pass
        # #endregion

    def _draft_axis_values(self, axis_idx: int) -> np.ndarray:
        v = np.asarray(self._draft_values, dtype=np.float64)
        if axis_idx >= v.ndim:
            return np.array([], dtype=np.float64)
        n = int(v.shape[axis_idx])
        a = self._draft_axes.get(axis_idx)
        if a is None or len(a) != n:
            a = np.arange(n, dtype=np.float64)
            self._draft_axes[axis_idx] = a.copy()
            if axis_idx not in self._baseline_axes:
                self._baseline_axes[axis_idx] = a.copy()
        return np.asarray(a, dtype=np.float64).reshape(-1)

    def _baseline_axis_values(self, axis_idx: int) -> np.ndarray:
        v = np.asarray(self._baseline_values, dtype=np.float64)
        if axis_idx >= v.ndim:
            return np.array([], dtype=np.float64)
        n = int(v.shape[axis_idx])
        a = self._baseline_axes.get(axis_idx)
        if a is None or len(a) != n:
            a = np.arange(n, dtype=np.float64)
            self._baseline_axes[axis_idx] = a.copy()
        return np.asarray(a, dtype=np.float64).reshape(-1)

    def editor_cell_kind(self, row: int, col: int) -> str:
        vals = np.asarray(self._draft_values, dtype=np.float64)
        if vals.ndim == 0:
            if row == 0 and col == 1:
                return "scalar"
            return "none"
        if vals.ndim == 1:
            n = int(vals.shape[0])
            if row == 0 and col == 0:
                return "none"
            if row == 1 and col == 0:
                return "none"
            if row == 0 and 1 <= col <= n:
                return "axis_x"
            if row == 1 and 1 <= col <= n:
                return "value"
            return "none"
        if vals.ndim == 2:
            nx, ny = int(vals.shape[0]), int(vals.shape[1])
            if row == 0 and col == 0:
                return "none"
            if row == 0 and 1 <= col <= ny:
                return "axis_x"
            if col == 0 and 1 <= row <= nx:
                return "axis_y"
            if 1 <= row <= nx and 1 <= col <= ny:
                return "value"
            return "none"
        return "none"

    def editor_has_numeric_selection(self) -> bool:
        sm = self._table.selectionModel()
        for ix in sm.selectedIndexes():
            k = self.editor_cell_kind(ix.row(), ix.column())
            if k in ("value", "axis_x", "axis_y", "scalar"):
                return True
        return False

    def editor_selection_is_homogeneous_numeric(self) -> bool:
        sm = self._table.selectionModel()
        idxs = sm.selectedIndexes()
        if not idxs:
            return False
        kinds = {self.editor_cell_kind(ix.row(), ix.column()) for ix in idxs}
        kinds.discard("none")
        if len(kinds) != 1:
            return False
        k0 = next(iter(kinds))
        return k0 in ("value", "axis_x", "axis_y", "scalar")

    def editor_digit_index_at(self, row: int, col: int, pos_in_cell) -> int | None:
        it = self._table.item(row, col)
        return digit_index_at_cell_pos(it, pos_in_cell, self._table)

    def _parse_user_float(self, text: str) -> float | None:
        return _parse_user_float_text(text)

    def _make_metadata_scroll_area(self, parent: QWidget, *, min_h: int = 180) -> QScrollArea:
        return build_calibration_metadata_scroll_area(parent, self._data, min_h=min_h)

    def _open_scalar_unified_dialog(self) -> None:
        """0-dim scalar: single Edit+Metadata dialog (shared implementation, no extra shell)."""
        vals = np.asarray(self._draft_values, dtype=np.float64)
        if vals.ndim != 0:
            return
        v = exec_scalar_calibration_edit_dialog(
            self.window(),
            self._data,
            window_icon=_host_window_icon(self),
        )
        if v is None:
            return
        self._set_draft_at_table_cell(0, 1, float(v))
        self._refresh_table_cells_from_draft()
        self._after_draft_mutation()

    def editor_begin_cell_edit(self, row: int, col: int) -> None:
        k = self.editor_cell_kind(row, col)
        if k not in ("value", "axis_x", "axis_y", "scalar"):
            return
        it = self._table.item(row, col)
        if it is None:
            return
        if k == "scalar":
            self._open_scalar_unified_dialog()
            return
        cur = it.text()
        title = "Edit value"
        text, ok = QInputDialog.getText(self.window(), title, "Number:", text=cur)
        if not ok:
            return
        v = self._parse_user_float(text)
        if v is None:
            QMessageBox.warning(self.window(), title, "Invalid number.")
            return
        self._set_draft_at_table_cell(row, col, float(v))
        self._refresh_table_cells_from_draft()
        self._after_draft_mutation()

    def editor_wheel_digit(self, row: int, col: int, digit_index: int, delta: int) -> None:
        it = self._table.item(row, col)
        if it is None:
            return
        new_s = adjust_digit_in_numeric_string(it.text(), digit_index, delta)
        if new_s is None:
            return
        v = self._parse_user_float(new_s)
        if v is None:
            return
        self._set_draft_at_table_cell(row, col, float(v))
        self._refresh_table_cells_from_draft()
        self._after_draft_mutation()

    def _draft_float_at_table_cell(self, row: int, col: int) -> float | None:
        k = self.editor_cell_kind(row, col)
        vals = np.asarray(self._draft_values, dtype=np.float64)
        if k == "scalar" and vals.ndim == 0:
            return float(vals.item())
        if k == "value":
            if vals.ndim == 1:
                return float(vals[col - 1])
            if vals.ndim == 2:
                return float(vals[row - 1, col - 1])
        if k == "axis_x":
            ax = self._draft_axis_values(1 if vals.ndim == 2 else 0)
            j = col - 1
            if 0 <= j < len(ax):
                return float(ax[j])
        if k == "axis_y":
            ax = self._draft_axis_values(0)
            i = row - 1
            if 0 <= i < len(ax):
                return float(ax[i])
        return None

    def _baseline_float_at_table_cell(self, row: int, col: int) -> float | None:
        k = self.editor_cell_kind(row, col)
        vals = np.asarray(self._baseline_values, dtype=np.float64)
        if k == "scalar" and vals.ndim == 0:
            return float(vals.item())
        if k == "value":
            if vals.ndim == 1:
                return float(vals[col - 1])
            if vals.ndim == 2:
                return float(vals[row - 1, col - 1])
        if k == "axis_x":
            ax = self._baseline_axis_values(1 if vals.ndim == 2 else 0)
            j = col - 1
            if 0 <= j < len(ax):
                return float(ax[j])
        if k == "axis_y":
            ax = self._baseline_axis_values(0)
            i = row - 1
            if 0 <= i < len(ax):
                return float(ax[i])
        return None

    def _set_draft_at_table_cell(self, row: int, col: int, value: float) -> None:
        k = self.editor_cell_kind(row, col)
        vals = self._draft_values
        if k == "scalar" and vals.ndim == 0:
            self._draft_values = np.array(float(value), dtype=np.float64)
            return
        if k == "value":
            if vals.ndim == 1:
                vals[col - 1] = value
            elif vals.ndim == 2:
                vals[row - 1, col - 1] = value
            return
        if k == "axis_x":
            ax = self._draft_axis_values(1 if vals.ndim == 2 else 0)
            j = col - 1
            if 0 <= j < len(ax):
                ax[j] = value
            return
        if k == "axis_y":
            ax = self._draft_axis_values(0)
            i = row - 1
            if 0 <= i < len(ax):
                ax[i] = value

    def _apply_op_to_float(self, v: float, op: str, operand: float) -> float:
        if op == "+":
            return v + operand
        if op == "-":
            return v - operand
        if op == "*":
            return v * operand
        if op == "/":
            return v / operand
        return v

    def editor_handle_bulk_operator(self, op: str) -> None:
        sm = self._table.selectionModel()
        idxs = [ix for ix in sm.selectedIndexes() if ix.isValid()]
        if not idxs:
            return
        k0 = self.editor_cell_kind(idxs[0].row(), idxs[0].column())
        if k0 not in ("value", "axis_x", "axis_y", "scalar"):
            return
        labels = {"+": "Add", "-": "Subtract", "*": "Multiply", "/": "Divide", "=": "Set value"}
        dlg_title = labels.get(op, "Operation")
        label = "Value:" if op == "=" else "Operand:"
        if op == "=":
            text, ok = QInputDialog.getText(self.window(), dlg_title, label, text="0")
            if not ok:
                return
            parsed = self._parse_user_float(text)
            if parsed is None:
                QMessageBox.warning(self.window(), dlg_title, "Invalid number.")
                return
            operand = float(np.float64(parsed))
        else:
            val, ok = QInputDialog.getDouble(self.window(), dlg_title, label, 0.0, -1e308, 1e308, 12)
            if not ok:
                return
            operand = float(val)
        if op == "/" and operand == 0.0:
            QMessageBox.warning(self.window(), dlg_title, "Division by zero is not allowed.")
            return
        for ix in idxs:
            r, c = ix.row(), ix.column()
            if self.editor_cell_kind(r, c) != k0:
                continue
            if op == "=":
                self._set_draft_at_table_cell(r, c, float(operand))
                continue
            cur = self._draft_float_at_table_cell(r, c)
            if cur is None or not np.isfinite(cur):
                continue
            new_v = self._apply_op_to_float(cur, op, operand)
            self._set_draft_at_table_cell(r, c, float(new_v))
        self._refresh_table_cells_from_draft()
        self._after_draft_mutation()

    def _refresh_table_cells_from_draft(self) -> None:
        vals = np.asarray(self._draft_values, dtype=np.float64)
        vmin, vmax = self._vmin_vmax()

        if vals.ndim == 0:
            it = self._table.item(0, 1)
            if it is not None:
                v0 = float(vals.item())
                dirty = _float_cell_dirty(v0, float(self._baseline_values.item()))
                it.setText(f"{v0:g}")
                self._style_data_cell(it, v0, (vmin, vmax), dirty)
            return

        if vals.ndim == 1:
            n = int(vals.shape[0])
            ax_x = self._draft_axis_values(0)
            bx = self._baseline_axis_values(0)
            for j in range(n):
                xj = float(ax_x[j]) if j < len(ax_x) else float(j)
                ita = self._table.item(0, j + 1)
                if ita is not None:
                    bj = float(bx[j]) if j < len(bx) else xj
                    ita.setText(f"{xj:g}")
                    self._style_axis_cell(ita, _float_cell_dirty(xj, bj))
                itv = self._table.item(1, j + 1)
                if itv is not None:
                    vj = float(vals[j])
                    itv.setText(f"{vj:g}")
                    self._style_data_cell(itv, vj, (vmin, vmax), _float_cell_dirty(vj, float(self._baseline_values[j])))
            return

        if vals.ndim == 2:
            nx, ny = int(vals.shape[0]), int(vals.shape[1])
            ax_y = self._draft_axis_values(0)
            ax_x = self._draft_axis_values(1)
            by = self._baseline_axis_values(0)
            bx = self._baseline_axis_values(1)
            for j in range(ny):
                xj = float(ax_x[j]) if j < len(ax_x) else float(j)
                itx = self._table.item(0, j + 1)
                if itx is not None:
                    bj = float(bx[j]) if j < len(bx) else xj
                    itx.setText(f"{xj:g}")
                    self._style_axis_cell(itx, _float_cell_dirty(xj, bj))
            for i in range(nx):
                yi = float(ax_y[i]) if i < len(ax_y) else float(i)
                ity = self._table.item(i + 1, 0)
                if ity is not None:
                    bi = float(by[i]) if i < len(by) else yi
                    ity.setText(f"{yi:g}")
                    self._style_axis_cell(ity, _float_cell_dirty(yi, bi))
                for j in range(ny):
                    vij = float(vals[i, j])
                    it = self._table.item(i + 1, j + 1)
                    if it is not None:
                        it.setText(f"{vij:g}")
                        self._style_data_cell(
                            it, vij, (vmin, vmax), _float_cell_dirty(vij, float(self._baseline_values[i, j]))
                        )

    def _style_axis_cell(self, item: QTableWidgetItem, dirty: bool) -> None:
        """Achsen wie im Original: Standard-Tabellenfarben; nur geänderte Werte blau/fett."""
        pal = self._table.palette()
        f = QFont(item.font())
        if dirty:
            item.setBackground(pal.brush(QPalette.ColorRole.Base))
            item.setForeground(_DIRTY_FOREGROUND)
            f.setBold(True)
        else:
            item.setBackground(QBrush())
            item.setForeground(QBrush())
            f.setBold(False)
        item.setFont(f)

    def _style_data_cell(
        self,
        item: QTableWidgetItem,
        value: float,
        mm: tuple[float, float],
        dirty: bool,
    ) -> None:
        bg, fg_heat = _heatmap_qcolor(value, mm[0], mm[1], self._cmap)
        item.setBackground(bg)
        fg = _DIRTY_FOREGROUND if dirty else fg_heat
        f = QFont(item.font())
        f.setBold(dirty)
        item.setForeground(fg)
        item.setFont(f)

    def _after_draft_mutation(self) -> None:
        self._update_pending_toolbar()
        if self._plot_visible and self._graph_plot_active and self._plot_rendered:
            self._draw_plot()

    def _on_apply_edits(self) -> None:
        self._baseline_values = np.array(self._draft_values, copy=True, dtype=np.float64)
        self._baseline_axes = {int(k): np.array(v, copy=True, dtype=np.float64) for k, v in self._draft_axes.items()}
        self._update_pending_toolbar()
        if self._plot_visible and self._graph_plot_active and self._plot_rendered:
            self._draw_plot()
        self._refresh_table_cells_from_draft()
        if self._on_applied_to_model is not None:
            self._on_applied_to_model(self)

    def applied_values_and_axes(self) -> tuple[np.ndarray, dict[int, np.ndarray]]:
        """Nach Apply: persistierte Entwurfsdaten (Baseline) für Modell/CCP."""
        v = np.asarray(self._baseline_values, dtype=np.float64)
        ax = {int(k): np.asarray(a, dtype=np.float64).copy() for k, a in self._baseline_axes.items()}
        return v, ax

    def _on_discard_edits(self) -> None:
        self._draft_values = np.array(self._baseline_values, copy=True, dtype=np.float64)
        self._draft_axes = {int(k): np.array(v, copy=True, dtype=np.float64) for k, v in self._baseline_axes.items()}
        self._refresh_table_cells_from_draft()
        self._update_pending_toolbar()
        if self._plot_visible and self._graph_plot_active and self._plot_rendered:
            self._draw_plot()

    def confirm_close_or_cancel(self) -> bool:
        """Return True if the window may close, False if the user cancels."""
        if not self._has_pending_edits():
            return True
        box = QMessageBox(self.window())
        _ico = _host_window_icon(self)
        if not _ico.isNull():
            box.setWindowIcon(_ico)
        box.setWindowTitle("Unsaved changes")
        box.setText("Apply or discard pending edits before closing?")
        apply_btn = box.addButton("Apply", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_btn or clicked is None:
            return False
        if clicked == apply_btn:
            self._on_apply_edits()
            return True
        if clicked == discard_btn:
            self._on_discard_edits()
            return True
        return False

    def _vmin_vmax(self) -> tuple[float, float]:
        v = np.asarray(self._draft_values, dtype=np.float64).ravel()
        v = v[np.isfinite(v)]
        if v.size == 0:
            return 0.0, 1.0
        return float(np.min(v)), float(np.max(v))

    def _build_table(self) -> None:
        d = self._data
        vals = np.asarray(self._draft_values, dtype=np.float64)
        vmin, vmax = self._vmin_vmax()
        axis0_label = d.axis_label(0, "x")
        axis1_label = d.axis_label(1, "y")
        value_label = d.value_label()
        fl = Qt.ItemFlag.ItemIsEnabled
        fn = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        self._table.horizontalHeader().setVisible(True)
        self._table.verticalHeader().setVisible(True)

        if vals.ndim == 0:
            self._table.setColumnCount(2)
            self._table.setRowCount(1)
            self._table.setHorizontalHeaderLabels(["Field", "Value"])
            it_t = QTableWidgetItem(d.title)
            it_t.setFlags(fl)
            self._table.setItem(0, 0, it_t)
            v0 = float(vals.item())
            bv0 = float(np.asarray(self._baseline_values, dtype=np.float64).item())
            itv = QTableWidgetItem(f"{v0:g}")
            itv.setFlags(fn)
            self._style_data_cell(itv, v0, (vmin, vmax), _float_cell_dirty(v0, bv0))
            self._table.setItem(0, 1, itv)
            self._hint.setText("Scalar — no plot. Double-click the value cell or use **Edit value…** in the toolbar.")
            self._graph_plot_active = False
            self._act_plot.setEnabled(False)
            self._canvas.hide()
            self._plot_scroll.hide()
            self._nav.hide()
            self._hint.show()
            self._apply_labeled_table_sizing()
            self._apply_outer_geometry()
            self._update_pending_toolbar()
            return

        if vals.ndim == 1:
            n = int(vals.shape[0])
            ax_x = self._draft_axis_values(0)
            bx = self._baseline_axis_values(0)
            self._table.horizontalHeader().setVisible(False)
            self._table.verticalHeader().setVisible(False)
            self._table.setRowCount(2)
            self._table.setColumnCount(n + 1)
            it_ax = QTableWidgetItem(axis0_label)
            it_ax.setFlags(fl)
            self._table.setItem(0, 0, it_ax)
            for j in range(n):
                xj = float(ax_x[j]) if j < len(ax_x) else float(j)
                bj = float(bx[j]) if j < len(bx) else xj
                ita = QTableWidgetItem(f"{xj:g}")
                ita.setFlags(fn)
                self._style_axis_cell(ita, _float_cell_dirty(xj, bj))
                self._table.setItem(0, j + 1, ita)
            it_vl = QTableWidgetItem(value_label)
            it_vl.setFlags(fl)
            self._table.setItem(1, 0, it_vl)
            for j in range(n):
                vj = float(vals[j])
                it = QTableWidgetItem(f"{vj:g}")
                it.setFlags(fn)
                self._style_data_cell(it, vj, (vmin, vmax), _float_cell_dirty(vj, float(self._baseline_values[j])))
                self._table.setItem(1, j + 1, it)
            self._graph_plot_active = True
            # #region agent log
            _it = self._table.item(1, 1)
            if _it is not None:
                _b = _it.background()
                _c = _b.color()
                _calmap_agent_log(
                    "A",
                    "widget._build_table:1d",
                    "after style before matrix_sizing",
                    {
                        "vmin": vmin,
                        "vmax": vmax,
                        "bg_style": getattr(_b.style(), "value", str(_b.style())),
                        "bg_rgb": [_c.red(), _c.green(), _c.blue()] if _c.isValid() else None,
                    },
                )
            # #endregion
            self._apply_matrix_table_sizing()
            self._apply_outer_geometry()
            self._update_pending_toolbar()
            return

        if vals.ndim == 2:
            nx, ny = int(vals.shape[0]), int(vals.shape[1])
            ax_y = self._draft_axis_values(0)
            ax_x = self._draft_axis_values(1)
            by = self._baseline_axis_values(0)
            bx = self._baseline_axis_values(1)
            self._table.horizontalHeader().setVisible(False)
            self._table.verticalHeader().setVisible(False)
            self._table.setRowCount(nx + 1)
            self._table.setColumnCount(ny + 1)
            it_corner = QTableWidgetItem(f"{axis1_label} / {axis0_label}")
            it_corner.setFlags(fl)
            self._table.setItem(0, 0, it_corner)
            for j in range(ny):
                xj = float(ax_x[j]) if j < len(ax_x) else float(j)
                bj = float(bx[j]) if j < len(bx) else xj
                itx = QTableWidgetItem(f"{xj:g}")
                itx.setFlags(fn)
                self._style_axis_cell(itx, _float_cell_dirty(xj, bj))
                self._table.setItem(0, j + 1, itx)
            for i in range(nx):
                yi = float(ax_y[i]) if i < len(ax_y) else float(i)
                bi = float(by[i]) if i < len(by) else yi
                ity = QTableWidgetItem(f"{yi:g}")
                ity.setFlags(fn)
                self._style_axis_cell(ity, _float_cell_dirty(yi, bi))
                self._table.setItem(i + 1, 0, ity)
                for j in range(ny):
                    vij = float(vals[i, j])
                    it = QTableWidgetItem(f"{vij:g}")
                    it.setFlags(fn)
                    self._style_data_cell(
                        it, vij, (vmin, vmax), _float_cell_dirty(vij, float(self._baseline_values[i, j]))
                    )
                    self._table.setItem(i + 1, j + 1, it)
            self._graph_plot_active = True
            # #region agent log
            _it = self._table.item(1, 1)
            if _it is not None:
                _b = _it.background()
                _c = _b.color()
                _calmap_agent_log(
                    "A",
                    "widget._build_table:2d",
                    "after style before matrix_sizing",
                    {
                        "vmin": vmin,
                        "vmax": vmax,
                        "bg_style": getattr(_b.style(), "value", str(_b.style())),
                        "bg_rgb": [_c.red(), _c.green(), _c.blue()] if _c.isValid() else None,
                    },
                )
            # #endregion
            self._apply_matrix_table_sizing()
            self._apply_outer_geometry()
            self._update_pending_toolbar()
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
        self._update_pending_toolbar()

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
        # #region agent log
        _it2 = self._table.item(1, 1)
        if _it2 is not None:
            _b2 = _it2.background()
            _c2 = _b2.color()
            _calmap_agent_log(
                "B",
                "widget._apply_matrix_table_sizing",
                "after align loop",
                {
                    "bg_style": getattr(_b2.style(), "value", str(_b2.style())),
                    "bg_rgb": [_c2.red(), _c2.green(), _c2.blue()] if _c2.isValid() else None,
                },
            )
        # #endregion

    def _apply_labeled_table_sizing(self) -> None:
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        vh = self._table.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(_TABLE_MATRIX_ROW_H)
        self._table.resizeRowsToContents()

    def _build_holoviews_element(self):
        d = self._data
        vals = np.asarray(self._draft_values, dtype=np.float64)
        vals_b = np.asarray(self._baseline_values, dtype=np.float64)
        title = f"{d.title} ({d.category})"
        axis0_label = d.axis_label(0, "x")
        axis1_label = d.axis_label(1, "y")
        value_label = d.value_label()
        overlay = self._has_pending_edits()

        if vals.ndim == 1:
            xs = self._draft_axis_values(0)
            if len(xs) != len(vals):
                xs = np.arange(len(vals), dtype=np.float64)
            curve = hv.Curve((xs, vals), [axis0_label], [value_label]).opts(
                opts.Curve(
                    title=title,
                    color=_1D_CURVE_COLOR,
                    linewidth=2,
                    xlabel=axis0_label,
                    ylabel=value_label,
                    show_grid=True,
                    fig_inches=_HV_2D_CURVE_FIG_INCHES,
                )
            )
            m = np.isfinite(xs) & np.isfinite(vals)
            support = (
                hv.Scatter((xs[m], vals[m]), [axis0_label], [value_label]).opts(_SUPPORT_SCATTER_OPTS)
                if m.any()
                else None
            )
            if not overlay:
                return curve * support if support is not None else curve
            xs_b = self._baseline_axis_values(0)
            if len(xs_b) != len(vals_b):
                xs_b = np.arange(len(vals_b), dtype=np.float64)
            m_b = np.isfinite(xs_b) & np.isfinite(vals_b)
            cur_b = hv.Curve((xs_b, vals_b), [axis0_label], [value_label]).opts(
                opts.Curve(
                    color=_BASELINE_CURVE_COLOR,
                    linewidth=2,
                    alpha=0.72,
                    fig_inches=_HV_2D_CURVE_FIG_INCHES,
                )
            )
            sup_b = (
                hv.Scatter((xs_b[m_b], vals_b[m_b]), [axis0_label], [value_label]).opts(
                    opts.Scatter(
                        s=34,
                        color=_BASELINE_SCATTER_COLOR,
                        edgecolors=_BASELINE_SCATTER_EDGE,
                        linewidth=1.0,
                    )
                )
                if m_b.any()
                else None
            )
            parts = [cur_b]
            if sup_b is not None:
                parts.append(sup_b)
            parts.append(curve)
            if support is not None:
                parts.append(support)
            el = parts[0]
            for p in parts[1:]:
                el = el * p
            return el

        if vals.ndim == 2:
            n0, n1 = int(vals.shape[0]), int(vals.shape[1])
            a0 = self._draft_axis_values(0)
            a1 = self._draft_axis_values(1)
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
                    xlabel=axis0_label,
                    ylabel=axis1_label,
                    zlabel=value_label,
                    azimuth=40,
                    elevation=30,
                )
            )
            x1_s = a0[L].ravel()
            x2_s = a1[K].ravel()
            support = (
                hv.Scatter3D(
                    (x1_s[fin], x2_s[fin], z_flat[fin]), kdims=[axis0_label, axis1_label, value_label]
                ).opts(_SUPPORT_SCATTER3D_OPTS)
                if fin.any()
                else None
            )
            if not overlay:
                return surf * support if support is not None else surf

            a0b = self._baseline_axis_values(0)
            a1b = self._baseline_axis_values(1)
            if len(a0b) != n0:
                a0b = np.arange(n0, dtype=np.float64)
            if len(a1b) != n1:
                a1b = np.arange(n1, dtype=np.float64)
            surf_b = hv.Surface((a0b, a1b, vals_b.T), kdims=[axis0_label, axis1_label], vdims=[value_label])
            surf_b = surf_b.opts(
                opts.Surface(
                    title="",
                    cmap=_BASELINE_SURFACE_CMAP,
                    colorbar=False,
                    projection="3d",
                    fig_inches=_HV_3D_FIG_INCHES,
                    xlabel=axis0_label,
                    ylabel=axis1_label,
                    zlabel=value_label,
                    azimuth=40,
                    elevation=30,
                )
            )
            z_fb = vals_b[L, K].ravel()
            x1b_s = a0b[L].ravel()
            x2b_s = a1b[K].ravel()
            fin_b = np.isfinite(x1b_s) & np.isfinite(x2b_s) & np.isfinite(z_fb)
            sup_b_opts = opts.Scatter3D(
                s=30,
                c=_BASELINE_SCATTER_COLOR,
                edgecolors=_BASELINE_SCATTER_EDGE,
                linewidth=0.8,
            )
            sup_b = (
                hv.Scatter3D(
                    (x1b_s[fin_b], x2b_s[fin_b], z_fb[fin_b]),
                    kdims=[axis0_label, axis1_label, value_label],
                ).opts(sup_b_opts)
                if fin_b.any()
                else None
            )
            parts2: list[object] = [surf_b]
            if sup_b is not None:
                parts2.append(sup_b)
            parts2.append(surf)
            if support is not None:
                parts2.append(support)
            el2 = parts2[0]
            for p in parts2[1:]:
                el2 = el2 * p
            return el2

        return None

    def _draw_plot(self) -> None:
        d = self._data
        vals = np.asarray(self._draft_values, dtype=np.float64)

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
            _pad_1d_curve_axes_for_markers(fig)
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
        vals = np.asarray(self._draft_values, dtype=np.float64)
        if vals.ndim == 0:
            self._open_scalar_unified_dialog()
            return
        dlg = QDialog(self.window())
        _ico = _host_window_icon(self)
        if not _ico.isNull():
            dlg.setWindowIcon(_ico)
        dlg.setWindowTitle("Parameter-Details")
        dlg.setModal(True)
        root = QVBoxLayout(dlg)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(self._make_metadata_scroll_area(dlg, min_h=240), 1)
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


class CalibrationMapCompareWidget(QWidget):
    """Read-only comparison of two 2D calibration maps."""

    def __init__(
        self,
        left_data: CalibrationMapData,
        right_data: CalibrationMapData,
        parent: QWidget | None = None,
        *,
        left_title: str = "A",
        right_title: str = "B",
    ) -> None:
        super().__init__(parent)
        self._left_data = left_data
        self._right_data = right_data
        self._left_title = left_title
        self._right_title = right_title
        # Intentionally very different color scales for faster visual discrimination.
        self._left_cmap = matplotlib.colormaps["autumn"]  # red -> yellow
        self._right_cmap = matplotlib.colormaps["winter"]  # green -> blue

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._toolbar = QToolBar()
        self._toolbar.setMovable(False)
        self._toolbar.setStyleSheet(studio_toolbar_stylesheet())
        icon_fg = QColor(STUDIO_TOOLBAR_FOREGROUND)
        self._act_details = QAction("Details", self)
        self._act_details.setIcon(icon_from_tinted_svg_file(_ICONS_DIR / "help-about-symbolic.svg", icon_fg))
        self._act_details.setToolTip("Show metadata of both variants")
        self._act_details.triggered.connect(self._on_show_details)
        self._toolbar.addAction(self._act_details)
        root.addWidget(self._toolbar, 0)

        self._left_table_title = QLabel(f"{left_title}: {left_data.title}")
        self._right_table_title = QLabel(f"{right_title}: {right_data.title}")
        self._left_table_title.setWordWrap(True)
        self._right_table_title.setWordWrap(True)
        _hf = QFont(self.font())
        _hf.setBold(True)
        self._left_table_title.setFont(_hf)
        self._right_table_title.setFont(_hf)

        self._left_table = QTableWidget(self)
        self._right_table = QTableWidget(self)
        for tw in (self._left_table, self._right_table):
            tw.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            tw.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            tw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            tw.setAlternatingRowColors(False)
            tw.setWordWrap(False)
            tw.horizontalHeader().setVisible(False)
            tw.verticalHeader().setVisible(False)

        self._tables_row = QWidget(self)
        tables_row_lay = QHBoxLayout(self._tables_row)
        tables_row_lay.setContentsMargins(8, 8, 8, 8)
        tables_row_lay.setSpacing(8)

        left_col = QWidget(self._tables_row)
        left_lay = QVBoxLayout(left_col)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)
        left_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        left_lay.addWidget(self._left_table_title, 0)
        left_lay.addWidget(self._left_table, 0)
        tables_row_lay.addWidget(left_col, 0, Qt.AlignmentFlag.AlignBottom)

        right_col = QWidget(self._tables_row)
        right_lay = QVBoxLayout(right_col)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)
        right_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        right_lay.addWidget(self._right_table_title, 0)
        right_lay.addWidget(self._right_table, 0)
        tables_row_lay.addWidget(right_col, 0, Qt.AlignmentFlag.AlignBottom)
        tables_row_lay.addStretch(1)
        root.addWidget(self._tables_row, 0)

        self._figure = Figure(figsize=(9.0, 4.8), layout="tight")
        self._canvas = FigureCanvasQTAgg(self._figure)
        self._canvas.installEventFilter(self)
        self._canvas.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        plot_col = QWidget(self)
        plot_lay = QVBoxLayout(plot_col)
        plot_lay.setContentsMargins(8, 0, 8, 8)
        plot_lay.setSpacing(0)
        self._plot_canvas_host = QWidget(plot_col)
        plot_lay.addWidget(self._plot_canvas_host, 1)
        self._canvas.setParent(self._plot_canvas_host)
        self._plot_canvas_host.installEventFilter(self)
        self._rotation_hint = QLabel("", self._plot_canvas_host)
        self._rotation_hint.setWordWrap(False)
        self._rotation_hint.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
        )
        # Über dem Plot (weiße Figure), nicht als Zeile unter dem Canvas vor schwarzem Fensterhintergrund.
        self._rotation_hint.setStyleSheet(
            "color: rgba(25, 25, 25, 245); font-size: 10px; background: transparent; padding: 2px 4px;"
        )
        self._rotation_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._rotation_hint.hide()
        root.addWidget(plot_col, 1)

        self._plot_payload: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None
        self._cmp_3d_rotation_cids: list[int] = []
        self._cmp_3d_rotation_drag: tuple[Axes3D, float, float] | None = None
        self._build_compare_view()
        QTimer.singleShot(0, self._sync_compare_canvas_host_geometry)

    def _sync_compare_canvas_host_geometry(self) -> None:
        """Canvas füllt den Host; nach Layout/Resize aufrufen."""
        h = self._plot_canvas_host
        if h.width() <= 0 or h.height() <= 0:
            return
        self._canvas.setGeometry(0, 0, h.width(), h.height())
        self._reposition_compare_rotation_hint()

    def _reposition_compare_rotation_hint(self) -> None:
        lbl = self._rotation_hint
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
        lbl.raise_()

    @staticmethod
    def _axis_and_values_2d(data: CalibrationMapData) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        vals = np.asarray(data.values, dtype=np.float64)
        if vals.ndim != 2:
            return np.array([], dtype=np.float64), np.array([], dtype=np.float64), vals
        ay = data.axis_values(0)
        ax = data.axis_values(1)
        if len(ay) != int(vals.shape[0]):
            ay = np.arange(int(vals.shape[0]), dtype=np.float64)
        if len(ax) != int(vals.shape[1]):
            ax = np.arange(int(vals.shape[1]), dtype=np.float64)
        return ay, ax, vals

    def _compare_table_intrinsic_wh(self, table: QTableWidget) -> tuple[int, int]:
        """Pixelgröße aus Zeilen-/Spaltenmaßen (Header ausgeblendet)."""
        ncols = table.columnCount()
        nrows = table.rowCount()
        min_px = _TABLE_MATRIX_COL_W * _PLOT_EXPAND_WIDTH_MATRIX_COLS + _TABLE_FRAME_PAD
        if ncols <= 0 or nrows <= 0:
            return min_px, _TABLE_MATRIX_ROW_H + _TABLE_FRAME_PAD
        cw_sum = sum(table.columnWidth(c) for c in range(ncols))
        rh_sum = sum(table.rowHeight(r) for r in range(nrows))
        vh = table.verticalHeader().width() if table.verticalHeader().isVisible() else 0
        hh = table.horizontalHeader().height() if table.horizontalHeader().isVisible() else 0
        intrinsic_w = cw_sum + vh + _TABLE_FRAME_PAD
        intrinsic_h = rh_sum + hh + _TABLE_FRAME_PAD
        return intrinsic_w, intrinsic_h

    def _apply_compare_tables_viewport_size(self) -> None:
        """Tabellen nur so groß wie Inhalt; Mindestbreite ≈ 6 Matrix-Spalten."""
        min_w = _TABLE_MATRIX_COL_W * _PLOT_EXPAND_WIDTH_MATRIX_COLS + _TABLE_FRAME_PAD
        for table, title in (
            (self._left_table, self._left_table_title),
            (self._right_table, self._right_table_title),
        ):
            iw, ih = self._compare_table_intrinsic_wh(table)
            fw = max(iw, min_w)
            table.setFixedSize(fw, ih)
            table.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            title.setMaximumWidth(fw)

    def _build_compare_view(self) -> None:
        ay_l, ax_l, vals_l = self._axis_and_values_2d(self._left_data)
        ay_r, ax_r, vals_r = self._axis_and_values_2d(self._right_data)
        if vals_l.ndim != 2 or vals_r.ndim != 2:
            msg_w = max(_TABLE_MATRIX_COL_W * _PLOT_EXPAND_WIDTH_MATRIX_COLS, 400)
            for tw in (self._left_table, self._right_table):
                tw.setRowCount(1)
                tw.setColumnCount(1)
                tw.setItem(0, 0, QTableWidgetItem("Comparison requires two 2D maps."))
                tw.setRowHeight(0, _TABLE_MATRIX_ROW_H * 2)
                tw.setColumnWidth(0, msg_w)
            self._apply_compare_tables_viewport_size()
            self._draw_plot(ay_l, ax_l, vals_l, ay_r, ax_r, vals_r)
            return

        ny_l, nx_l = int(vals_l.shape[0]), int(vals_l.shape[1])
        ny_r, nx_r = int(vals_r.shape[0]), int(vals_r.shape[1])
        max_nx = max(nx_l, nx_r)
        max_ny = max(ny_l, ny_r)

        axis_x_diff = np.ones(max_nx, dtype=bool)
        axis_y_diff = np.ones(max_ny, dtype=bool)
        val_diff = np.ones((max_ny, max_nx), dtype=bool)

        min_nx = min(nx_l, nx_r)
        min_ny = min(ny_l, ny_r)
        if min_nx > 0:
            axis_x_diff[:min_nx] = [_float_cell_dirty(float(ax_l[j]), float(ax_r[j])) for j in range(min_nx)]
        if min_ny > 0:
            axis_y_diff[:min_ny] = [_float_cell_dirty(float(ay_l[i]), float(ay_r[i])) for i in range(min_ny)]
        if min_nx > 0 and min_ny > 0:
            for i in range(min_ny):
                for j in range(min_nx):
                    val_diff[i, j] = _float_cell_dirty(float(vals_l[i, j]), float(vals_r[i, j]))

        self._build_single_compare_table(
            self._left_table, self._left_data, ay_l, ax_l, vals_l, axis_y_diff, axis_x_diff, val_diff, self._left_cmap
        )
        self._build_single_compare_table(
            self._right_table,
            self._right_data,
            ay_r,
            ax_r,
            vals_r,
            axis_y_diff,
            axis_x_diff,
            val_diff,
            self._right_cmap,
        )
        self._plot_payload = (ay_l, ax_l, vals_l, ay_r, ax_r, vals_r)
        self._apply_compare_tables_viewport_size()
        self._draw_plot(ay_l, ax_l, vals_l, ay_r, ax_r, vals_r)

    def _redraw_compare_plot(self) -> None:
        if self._plot_payload is None:
            return
        ay_l, ax_l, vals_l, ay_r, ax_r, vals_r = self._plot_payload
        self._draw_plot(ay_l, ax_l, vals_l, ay_r, ax_r, vals_r)

    def _set_rotation_hint_visible(self, show: bool) -> None:
        if not show:
            self._rotation_hint.hide()
            return
        self._rotation_hint.setText(_3D_ROTATION_CTRL_HINT)
        self._rotation_hint.show()
        self._rotation_hint.raise_()
        self._reposition_compare_rotation_hint()

    def _disconnect_compare_rotation_callbacks(self) -> None:
        fig = self._canvas.figure
        if fig is not None:
            canvas = fig.canvas
            for cid in self._cmp_3d_rotation_cids:
                try:
                    canvas.mpl_disconnect(cid)
                except Exception:
                    pass
        self._cmp_3d_rotation_cids.clear()
        self._cmp_3d_rotation_drag = None
        self._rotation_hint.hide()

    def _connect_compare_rotation_callbacks(self, fig: Figure) -> None:
        self._disconnect_compare_rotation_callbacks()
        canvas = fig.canvas
        self._cmp_3d_rotation_cids = [
            canvas.mpl_connect("button_press_event", self._on_cmp_3d_rotation_press),
            canvas.mpl_connect("motion_notify_event", self._on_cmp_3d_rotation_motion),
            canvas.mpl_connect("button_release_event", self._on_cmp_3d_rotation_release),
        ]

    def _on_cmp_3d_rotation_press(self, event: object) -> None:
        btn = getattr(event, "button", None)
        if btn != MouseButton.LEFT and btn != 1:
            return
        ax = event.inaxes
        if not isinstance(ax, Axes3D):
            return
        if event.xdata is None or event.ydata is None:
            return
        self._cmp_3d_rotation_drag = (ax, float(event.xdata), float(event.ydata))
        self._set_rotation_hint_visible(True)

    def _on_cmp_3d_rotation_motion(self, event: object) -> None:
        if self._cmp_3d_rotation_drag is None:
            return
        buttons = getattr(event, "buttons", frozenset())
        if not (MouseButton.LEFT in buttons or 1 in buttons):
            return
        ax, lx, ly = self._cmp_3d_rotation_drag
        if event.inaxes is not ax or event.xdata is None or event.ydata is None:
            return
        dx = float(event.xdata) - lx
        dy = float(event.ydata) - ly
        if dx == 0.0 and dy == 0.0:
            return
        full_rot = _synarius_3d_full_rotation_requested(event, turntable_lock=True)
        _apply_synarius_3d_rotation_delta(ax, dx, dy, full_rotation=full_rot)
        self._cmp_3d_rotation_drag = (ax, float(event.xdata), float(event.ydata))
        self._set_rotation_hint_visible(True)
        ax.stale = True
        event.canvas.draw_idle()

    def _on_cmp_3d_rotation_release(self, event: object) -> None:
        btn = getattr(event, "button", None)
        if btn == MouseButton.LEFT or btn == 1:
            self._cmp_3d_rotation_drag = None
            self._rotation_hint.hide()

    @staticmethod
    def _apply_compare_bold(item: QTableWidgetItem, is_diff: bool) -> None:
        f = QFont(item.font())
        f.setBold(bool(is_diff))
        item.setFont(f)

    def _build_single_compare_table(
        self,
        table: QTableWidget,
        data: CalibrationMapData,
        ay: np.ndarray,
        ax: np.ndarray,
        vals: np.ndarray,
        axis_y_diff: np.ndarray,
        axis_x_diff: np.ndarray,
        val_diff: np.ndarray,
        cmap,
    ) -> None:
        ny, nx = int(vals.shape[0]), int(vals.shape[1])
        axis0_label = data.axis_label(0, "x")
        axis1_label = data.axis_label(1, "y")
        table.setRowCount(ny + 1)
        table.setColumnCount(nx + 1)
        fl = Qt.ItemFlag.ItemIsEnabled
        fn = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        it_corner = QTableWidgetItem(f"{axis1_label} / {axis0_label}")
        it_corner.setFlags(fl)
        table.setItem(0, 0, it_corner)

        vflat = np.asarray(vals, dtype=np.float64).ravel()
        finite = vflat[np.isfinite(vflat)]
        if finite.size == 0:
            vmin, vmax = 0.0, 1.0
        else:
            vmin, vmax = float(np.min(finite)), float(np.max(finite))

        for j in range(nx):
            itx = QTableWidgetItem(f"{float(ax[j]):g}")
            itx.setFlags(fn)
            self._apply_compare_bold(itx, bool(axis_x_diff[j]) if j < len(axis_x_diff) else True)
            table.setItem(0, j + 1, itx)
        for i in range(ny):
            ity = QTableWidgetItem(f"{float(ay[i]):g}")
            ity.setFlags(fn)
            self._apply_compare_bold(ity, bool(axis_y_diff[i]) if i < len(axis_y_diff) else True)
            table.setItem(i + 1, 0, ity)
            for j in range(nx):
                v = float(vals[i, j])
                it = QTableWidgetItem(f"{v:g}")
                it.setFlags(fn)
                bg, fg = _heatmap_qcolor(v, vmin, vmax, cmap)
                it.setBackground(bg)
                it.setForeground(fg)
                self._apply_compare_bold(it, bool(val_diff[i, j]) if (i < val_diff.shape[0] and j < val_diff.shape[1]) else True)
                table.setItem(i + 1, j + 1, it)

        for r in range(table.rowCount()):
            table.setRowHeight(r, _TABLE_MATRIX_ROW_H)
        for c in range(table.columnCount()):
            table.setColumnWidth(c, _TABLE_MATRIX_COL_W)
        for r in range(table.rowCount()):
            for c in range(table.columnCount()):
                it = table.item(r, c)
                if it is not None:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _draw_plot(
        self,
        ay_l: np.ndarray,
        ax_l: np.ndarray,
        vals_l: np.ndarray,
        ay_r: np.ndarray,
        ax_r: np.ndarray,
        vals_r: np.ndarray,
    ) -> None:
        fig = self._figure
        self._disconnect_compare_rotation_callbacks()
        fig.clear()
        try:
            # Manual colorbar axes for two side-by-side legends.
            fig.set_layout_engine(None)
        except Exception:
            pass
        ax = fig.add_subplot(111, projection="3d")

        def _mesh(ay: np.ndarray, axv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            xx, yy = np.meshgrid(axv, ay)
            return xx, yy

        surf_l = None
        surf_r = None
        if vals_l.ndim == 2 and vals_l.size > 0:
            xl, yl = _mesh(ay_l, ax_l)
            surf_l = ax.plot_surface(
                xl, yl, vals_l, cmap=self._left_cmap.name, alpha=0.68, linewidth=0, antialiased=True
            )
            zl = vals_l.ravel()
            fl = np.isfinite(xl.ravel()) & np.isfinite(yl.ravel()) & np.isfinite(zl)
            if fl.any():
                ax.scatter(
                    xl.ravel()[fl],
                    yl.ravel()[fl],
                    zl[fl],
                    c=_SUPPORT_NEW_MARKER_COLOR,
                    edgecolors=_SUPPORT_NEW_MARKER_EDGE,
                    s=38,
                    linewidths=1.0,
                    depthshade=False,
                    marker="o",
                )
        if vals_r.ndim == 2 and vals_r.size > 0:
            xr, yr = _mesh(ay_r, ax_r)
            surf_r = ax.plot_surface(
                xr, yr, vals_r, cmap=self._right_cmap.name, alpha=0.50, linewidth=0, antialiased=True
            )
            zr = vals_r.ravel()
            fr = np.isfinite(xr.ravel()) & np.isfinite(yr.ravel()) & np.isfinite(zr)
            if fr.any():
                ax.scatter(
                    xr.ravel()[fr],
                    yr.ravel()[fr],
                    zr[fr],
                    c=_SUPPORT_NEW_MARKER_COLOR,
                    edgecolors=_SUPPORT_NEW_MARKER_EDGE,
                    s=38,
                    linewidths=1.0,
                    depthshade=False,
                    marker="o",
                )

        ax.set_xlabel(self._left_data.axis_label(1, "x"))
        ax.set_ylabel(self._left_data.axis_label(0, "y"))
        ax.set_zlabel(self._left_data.value_label())
        _enable_3d_mouse_rotation(fig)
        self._connect_compare_rotation_callbacks(fig)
        # Linke Legende am linken Rand, rechte am rechten Rand; gleicher Abstand M
        # vom Canvas-Rand zur Colorbar bzw. spiegelbildlich zum Beschriftungsraum außen.
        M = 0.06
        cbar_w = 0.022
        pad_cbar_to_plot = 0.02
        cbar_y = 0.17
        cbar_h = 0.68
        ax_y0, ax_h = 0.11, 0.80
        n_cb = int(surf_l is not None) + int(surf_r is not None)
        def _compare_cbar_tick_inward(cbar: object, *, side: str) -> None:
            """Skalen-Zahlen zur 3D-Achse; Rand M bleibt für die Colorbar-Leiste frei."""
            try:
                cax = cbar.ax
                if side == "left":
                    cax.yaxis.set_ticks_position("right")
                    cax.tick_params(axis="y", labelleft=False, labelright=True, pad=2)
                else:
                    cax.yaxis.set_ticks_position("left")
                    cax.tick_params(axis="y", labelleft=True, labelright=False, pad=2)
            except Exception:
                pass

        if n_cb == 0:
            ax.set_position([M, ax_y0, 1.0 - 2 * M, ax_h])
        elif n_cb == 1:
            if surf_l is not None:
                cax_l = fig.add_axes([M, cbar_y, cbar_w, cbar_h])
                cbar_l = fig.colorbar(surf_l, cax=cax_l)
                cbar_l.set_label(self._left_title)
                _compare_cbar_tick_inward(cbar_l, side="left")
                x0 = M + cbar_w + pad_cbar_to_plot
                ax.set_position([x0, ax_y0, 1.0 - x0 - M, ax_h])
            else:
                cax_r = fig.add_axes([1.0 - M - cbar_w, cbar_y, cbar_w, cbar_h])
                cbar_r = fig.colorbar(surf_r, cax=cax_r)
                cbar_r.set_label(self._right_title)
                _compare_cbar_tick_inward(cbar_r, side="right")
                x1 = 1.0 - M - cbar_w - pad_cbar_to_plot
                ax.set_position([M, ax_y0, x1 - M, ax_h])
        else:
            cax_l = fig.add_axes([M, cbar_y, cbar_w, cbar_h])
            cbar_l = fig.colorbar(surf_l, cax=cax_l)
            cbar_l.set_label(self._left_title)
            _compare_cbar_tick_inward(cbar_l, side="left")
            cax_r = fig.add_axes([1.0 - M - cbar_w, cbar_y, cbar_w, cbar_h])
            cbar_r = fig.colorbar(surf_r, cax=cax_r)
            cbar_r.set_label(self._right_title)
            _compare_cbar_tick_inward(cbar_r, side="right")
            x_plot0 = M + cbar_w + pad_cbar_to_plot
            x_plot1 = 1.0 - M - cbar_w - pad_cbar_to_plot
            ax.set_position([x_plot0, ax_y0, x_plot1 - x_plot0, ax_h])
        if hasattr(ax, "set_navigate_mode"):
            ax.set_navigate_mode(None)
        _patch_axes3d_preserve_dist(ax)
        _apply_3d_initial_zoom_like_wheel(ax, _3D_INITIAL_WHEEL_ZOOM_TICKS)
        self._canvas.draw()
        _relax_3d_plot_clipping(fig)
        _apply_3d_figure_suptitle(fig, f"{self._left_data.title} vs {self._right_data.title}")
        self._canvas.draw_idle()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._plot_canvas_host and event.type() == QEvent.Type.Resize:
            self._sync_compare_canvas_host_geometry()
            return False
        if watched is self._canvas and event.type() == QEvent.Type.Wheel:
            wheel = cast(QWheelEvent, event)
            dy = wheel.angleDelta().y()
            if dy == 0:
                return False
            for ax in self._figure.axes:
                if isinstance(ax, Axes3D):
                    if dy > 0:
                        ax._dist /= _3D_SCROLL_ZOOM_STEP
                    else:
                        ax._dist *= _3D_SCROLL_ZOOM_STEP
                    ax._dist = max(_3D_DIST_MIN, min(_3D_DIST_MAX, ax._dist))
                    ax.stale = True
            _relax_3d_plot_clipping(self._figure)
            self._canvas.draw()
            return True
        return super().eventFilter(watched, event)

    def _on_show_details(self) -> None:
        dlg = QDialog(self.window())
        _ico = _host_window_icon(self)
        if not _ico.isNull():
            dlg.setWindowIcon(_ico)
        dlg.setWindowTitle("Parameter-Details (Vergleich)")
        dlg.setModal(True)
        root = QVBoxLayout(dlg)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        tabs = QTabWidget(dlg)
        tabs.addTab(build_calibration_metadata_scroll_area(dlg, self._left_data, min_h=220), self._left_title)
        tabs.addTab(build_calibration_metadata_scroll_area(dlg, self._right_data, min_h=220), self._right_title)
        root.addWidget(tabs, 1)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(dlg.accept)
        root.addWidget(btns)
        dlg.resize(620, 420)
        dlg.exec()


class CalibrationMapCompareShell(QWidget):
    """Host wrapper for :class:`CalibrationMapCompareWidget`."""

    def __init__(
        self,
        left_data: CalibrationMapData,
        right_data: CalibrationMapData,
        parent: QWidget | None = None,
        *,
        left_title: str = "A",
        right_title: str = "B",
    ) -> None:
        super().__init__(parent)
        self._viewer = CalibrationMapCompareWidget(
            left_data, right_data, self, left_title=left_title, right_title=right_title
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._viewer)

    def sizeHint(self) -> QSize:
        return self._viewer.sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self._viewer.minimumSizeHint()

    @property
    def viewer(self) -> CalibrationMapCompareWidget:
        return self._viewer


def create_calibration_map_compare_viewer(
    left_data: CalibrationMapData,
    right_data: CalibrationMapData,
    *,
    parent: QWidget | None = None,
    embedded: bool = True,
    left_title: str = "A",
    right_title: str = "B",
) -> CalibrationMapCompareShell | CalibrationMapCompareWidget:
    """Return read-only comparison viewer for two calibration maps."""
    if embedded:
        return CalibrationMapCompareShell(
            left_data, right_data, parent, left_title=left_title, right_title=right_title
        )
    return CalibrationMapCompareWidget(
        left_data, right_data, parent, left_title=left_title, right_title=right_title
    )


class _CalmapCloseGuard(QObject):
    """Intercepts QDialog close while calibration edits are pending."""

    def __init__(self, dlg: QDialog, viewer: CalibrationMapWidget) -> None:
        super().__init__(dlg)
        self._dlg = dlg
        self._viewer = viewer
        dlg.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._dlg and event.type() == QEvent.Type.Close:
            if isinstance(event, QCloseEvent):
                if not self._viewer.confirm_close_or_cancel():
                    event.ignore()
                    return True
        return super().eventFilter(watched, event)


class CalibrationMapShell(QWidget):
    """Thin host embedding :class:`CalibrationMapWidget` (same pattern as :class:`DataViewerShell`)."""

    def __init__(
        self,
        data: CalibrationMapData,
        parent: QWidget | None = None,
        *,
        on_applied_to_model: Callable[[CalibrationMapWidget], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = CalibrationMapWidget(data, self, on_applied_to_model=on_applied_to_model)
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

    def attach_dialog_close_guard(self, dlg: QDialog) -> None:
        """Call once for the ParaWiz dialog so close (X) prompts Apply / Discard / Cancel."""
        _CalmapCloseGuard(dlg, self._viewer)
