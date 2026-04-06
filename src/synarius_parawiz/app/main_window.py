"""Main window for Synarius ParaWiz."""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QMouseEvent,
    QPalette,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from synarius_core.controller import CommandError, MinimalController
from synarius_core.model.data_model import ComplexInstance
from synarius_core.parameters.repository import ParameterRecord, ParametersRepository

import synarius_parawiz as _synarius_parawiz_pkg
from synarius_dataviewer.app import theme
from synarius_parawiz._version import __version__
from synarius_parawiz.app.console_window import ConsoleWindow
from synarius_parawiz.app.icon_utils import parawiz_app_icon
from synarius_parawiz.app.status_progress_widget import StatusMessageProgressBar
from synariustools.tools.plotwidget.plot_theme import studio_commit_toolbutton_widget_stylesheet
from synariustools.tools.plotwidget.svg_icons import icon_from_tinted_svg_file

@dataclass(slots=True)
class _ParawizRowCrossDsStyle:
    """Vergleich eines Parameters über mehrere Parametersätze (Zeilenformatierung)."""

    row_bold: bool
    star_suffix: bool
    dataset_col_fg: dict[int, QColor]
    frozen_fg: QColor | None


_PARAWIZ_ROW_STYLE_NEUTRAL = _ParawizRowCrossDsStyle(False, False, {}, None)
# Schriftfarben (nicht schwarz): je Cluster unterscheidbar, auf hellem Zellenhintergrund lesbar
_PARAWIZ_DIFF_CLUSTER_HEX = (
    "#b45309",
    "#1d4ed8",
    "#7c3aed",
    "#047857",
    "#b91c1c",
    "#a21caf",
)
# Name-Spalte, wenn sich die Sätze unterscheiden (neutral zur Zuordnung der Satz-Spalten)
_PARAWIZ_NAME_COL_MIXED_HEX = "#4b5563"


def _parameter_name_matches_filter(name: str, pattern: str) -> bool:
    """Substring match; if the pattern contains ``*`` or ``?``, use shell-style glob (case-insensitive)."""
    p = pattern.strip()
    if not p:
        return True
    nl = name.lower()
    pl = p.lower()
    if "*" in pl or "?" in pl:
        return fnmatch.fnmatch(nl, pl)
    return pl in nl


class MainWindow(QMainWindow):
    # CCP-Zeilen für Kennfeld/Kennlinie: in-process ohne Shell-Limits, dennoch abgesichert
    _PARAWIZ_CCP_CMD_MAX_TOTAL = 280_000
    _parawiz_missing_ds_brush_cached: QBrush | None = None
    _PARAWIZ_COL_RESIZE_EDGE_PX = 8

    # Status bar: show busy/determinate progress for larger DCM loads
    _DCM_PARSE_PROGRESS_MIN_BYTES = 100 * 1024
    _DCM_IMPORT_PROGRESS_MIN_SPECS = 30
    # Zielanteil der Leiste für Tabellen-Aufbau (nach 3*n); skaliert mit n, sonst kaum sichtbar.
    _DCM_TABLE_BAR_SHARE = 0.18
    _DCM_TABLE_BAR_SLOTS_MIN = 500
    # Wide enough for full German status lines, e.g. "DCM · Import 9999 / 10000"
    _DCM_STATUS_PROGRESS_WIDTH = 340
    _DCM_STATUS_PROGRESS_INNER_HEIGHT = 12
    # Zwei Zeilen in der fixen Kopftabelle (setSpan), scrollt nicht mit dem Datenbereich.
    PARAWIZ_TABLE_HEADER_ROWS = 2
    # Viewport der ScrollArea oft noch ~100px breit während populate läuft — dann kein Pad/Clamp,
    # sonst werden Spalten zerquetscht und host_w = min(total,vp) clippt den Inhalt.
    _PARAWIZ_SCROLL_VP_WIDTH_READY_MIN = 160

    _DATASET_HEADER_COLORS: tuple[str, ...] = (
        "#2f5875",
        "#4e3b76",
        "#226a58",
        "#7a5030",
        "#2d5b4d",
        "#5f3e57",
        "#3f4f72",
        "#6b5a31",
    )

    @staticmethod
    def _parawiz_cross_dataset_filter_button_stylesheet() -> str:
        """Gleiches QSS wie Apply im CalmapWidget; aktiver Filter mit sichtbarem Rand."""
        return studio_commit_toolbutton_widget_stylesheet() + (
            "QToolButton:checked { border: 2px solid #ffffff; }"
            "QToolButton:!checked { border: none; }"
        )

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
        self._cached_datasets: list[tuple[str, UUID]] = []
        self._cached_rows: list[tuple[str, dict[UUID, tuple[str, str, UUID]]]] = []
        self._cached_row_styles: dict[str, _ParawizRowCrossDsStyle] = {}
        self._parawiz_resize_split_i: int | None = None
        #: ``pair`` = zwei angrenzende Spalten; ``tail`` = nur rechte Kante der letzten Spalte
        self._parawiz_resize_scroll_mode: str | None = None
        self._parawiz_resize_frozen_drag = False
        self._parawiz_resize_start_gx = 0
        self._parawiz_resize_w0 = 0
        self._parawiz_resize_w1 = 0
        self._parawiz_compare_first_pid: UUID | None = None

        self._filter_name = QLineEdit(self)
        self._filter_name.setPlaceholderText("Filter by parameter name… (* ? wildcards)")
        self._filter_name.textChanged.connect(self._apply_filter_to_table)
        self._filter_name.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        _fpal = self._filter_name.palette()
        _fpal.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        _fpal.setColor(QPalette.ColorRole.Text, QColor("#1a1a1a"))
        self._filter_name.setPalette(_fpal)
        self._filter_name.setAutoFillBackground(True)
        self._filter_name.setStyleSheet(
            "QLineEdit {"
            " background-color: #ffffff;"
            " color: #1a1a1a;"
            " border: 1px solid #b8b8b8;"
            " border-radius: 2px;"
            " padding: 3px 8px;"
            "}"
            "QLineEdit:focus { border: 1px solid #586cd4; }"
        )

        self._filter_count_label = QLabel("", self)
        self._filter_count_label.setStyleSheet("color: #ffffff; font-size: 11px; padding: 2px 0px;")
        self._filter_row = QWidget(self)
        _filter_row_lay = QHBoxLayout(self._filter_row)
        _filter_row_lay.setContentsMargins(0, 0, 0, 0)
        _filter_row_lay.setSpacing(10)
        _filter_row_lay.addWidget(self._filter_name, 0)
        _filter_row_lay.addWidget(self._filter_count_label, 0, Qt.AlignmentFlag.AlignVCenter)

        def _mk_filter_toggle_btn(text: str, obj_name: str, tip: str, breeze_svg: str) -> QToolButton:
            b = QToolButton(self._filter_row)
            b.setText(text)
            b.setObjectName(obj_name)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setAutoRaise(False)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            b.setStyleSheet(MainWindow._parawiz_cross_dataset_filter_button_stylesheet())
            _ip = MainWindow._parawiz_breeze_icons_dir() / breeze_svg
            if _ip.is_file():
                _fg = QColor(theme.STUDIO_TOOLBAR_FOREGROUND)
                b.setIcon(icon_from_tinted_svg_file(_ip, _fg, logical_side=18))
            b.setEnabled(False)
            return b

        self._btn_filter_hide_unequal = _mk_filter_toggle_btn(
            "Gleiche",
            "ParaWizFilterHideUnequal",
            "Nur Zeilen, in denen Werte, Achsen und Metadaten (ohne UUIDs) in allen Parametersätzen "
            "übereinstimmen. Mit dem Namensfilter per UND verknüpft.",
            "dialog-ok-apply.svg",
        )
        self._btn_filter_hide_equal = _mk_filter_toggle_btn(
            "Abweichende",
            "ParaWizFilterHideEqual",
            "Nur Zeilen mit Unterschieden zwischen Parametersätzen (Werte, Achsen oder Metadaten). "
            "Mit dem Namensfilter per UND verknüpft.",
            "vcs-diff.svg",
        )
        self._btn_filter_hide_unequal.toggled.connect(self._on_cross_dataset_filter_toggled)
        self._btn_filter_hide_equal.toggled.connect(self._on_cross_dataset_filter_toggled)
        _filter_row_lay.addWidget(self._btn_filter_hide_unequal, 0, Qt.AlignmentFlag.AlignVCenter)
        _filter_row_lay.addWidget(self._btn_filter_hide_equal, 0, Qt.AlignmentFlag.AlignVCenter)

        _filter_row_lay.addStretch(1)

        self._parawiz_header_scroll_guard = False
        self._parawiz_vscroll_guard = False
        self._parawiz_sel_guard = False

        def _mk_param_table(*, name: str) -> QTableWidget:
            t = QTableWidget(self)
            t.setObjectName(name)
            t.setColumnCount(1)
            t.horizontalHeader().setVisible(False)
            t.verticalHeader().setVisible(False)
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.setShowGrid(False)
            t.horizontalHeader().setStretchLastSection(False)
            t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
            return t

        self._table_header_frozen = _mk_param_table(name="ParameterTableFrozenHeader")
        self._table_header_frozen.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
        self._table_header_frozen.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table_header_frozen.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table_header_frozen.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table_header_frozen.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._table_frozen = _mk_param_table(name="ParameterTableFrozen")
        self._table_frozen.setAlternatingRowColors(True)
        self._table_frozen.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_frozen.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table_frozen.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table_frozen.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table_frozen.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        self._frozen_block = QWidget(self)
        _fb_lay = QVBoxLayout(self._frozen_block)
        _fb_lay.setContentsMargins(0, 0, 0, 0)
        _fb_lay.setSpacing(0)
        _fb_lay.addWidget(self._table_header_frozen, 0)
        _fb_lay.addWidget(self._table_frozen, 1)
        self._frozen_block.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._frozen_block.setStyleSheet(
            f"QWidget {{ background-color: {theme.CONSOLE_CHROME_BACKGROUND}; }}"
        )

        self._table_header = _mk_param_table(name="ParameterTableHeader")
        self._table_header.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
        self._table_header.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table_header.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table_header.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Minimum horizontal: sonst kann das Layout die Viewport-Breite < Spaltensumme + VScroll geben
        # und bei horizontalem ScrollBarPolicy Off werden rechte Spalten (2. Parametersatz) abgeschnitten.
        self._table_header.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

        self._table = _mk_param_table(name="ParameterTable")
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table_header.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _corner = QWidget(self._table)
        _corner.setAutoFillBackground(True)
        _cpal = _corner.palette()
        _cpal.setColor(QPalette.ColorRole.Window, QColor("#2f2f2f"))
        _corner.setPalette(_cpal)
        self._table.setCornerWidget(_corner)

        self._param_table_host = QWidget(self)
        _ph_lay = QVBoxLayout(self._param_table_host)
        _ph_lay.setContentsMargins(0, 0, 0, 0)
        _ph_lay.setSpacing(0)
        _ph_lay.addWidget(self._table_header, 0)
        _ph_lay.addWidget(self._table, 1)
        self._param_table_host.setStyleSheet(
            f"QWidget {{ background-color: {theme.CONSOLE_CHROME_BACKGROUND}; }}"
        )

        self._param_table_scroll = QScrollArea(self)
        self._param_table_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._param_table_scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background-color: {theme.CONSOLE_CHROME_BACKGROUND}; }}"
        )
        self._param_table_scroll.setWidgetResizable(False)
        self._param_table_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._param_table_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._param_table_scroll.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._param_table_scroll.setMinimumHeight(200)
        self._param_table_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._param_table_scroll.setWidget(self._param_table_host)
        _pt_vp = self._param_table_scroll.viewport()
        if _pt_vp is not None:
            _pt_vp.setAutoFillBackground(True)
            _pt_vp_pal = _pt_vp.palette()
            _pt_vp_pal.setColor(QPalette.ColorRole.Window, QColor(theme.CONSOLE_CHROME_BACKGROUND))
            _pt_vp.setPalette(_pt_vp_pal)

        _table_row = QWidget(self)
        _table_row.setStyleSheet(
            f"QWidget {{ background-color: {theme.CONSOLE_CHROME_BACKGROUND}; }}"
        )
        _table_row_lay = QHBoxLayout(_table_row)
        _table_row_lay.setContentsMargins(0, 0, 0, 0)
        _table_row_lay.setSpacing(0)
        # Kein AlignTop: sonst füllt der Block nicht die Zeilenhöhe → darunter sichtbarer „Loch“-Bereich (schwarz).
        _table_row_lay.addWidget(self._frozen_block, 0, Qt.AlignmentFlag.AlignLeft)
        # Stretch 1: sonst nutzt die ScrollArea nur die Mindestbreite des Inhalts → kollabiert auf einen Streifen.
        _table_row_lay.addWidget(self._param_table_scroll, 1)

        central = QWidget(self)
        central.setAutoFillBackground(True)
        _cpal_c = central.palette()
        _cpal_c.setColor(QPalette.ColorRole.Window, QColor(theme.CONSOLE_CHROME_BACKGROUND))
        central.setPalette(_cpal_c)
        central.setStyleSheet(
            f"QWidget#ParaWizCentral {{ background-color: {theme.CONSOLE_CHROME_BACKGROUND}; }}"
        )
        central.setObjectName("ParaWizCentral")
        lay = QVBoxLayout(central)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
        lay.addWidget(self._filter_row, 0, Qt.AlignmentFlag.AlignLeft)
        self._table_row = _table_row
        self._table_row.setVisible(False)
        self._filter_row.setVisible(False)
        lay.addWidget(self._table_row, 1)
        self.setCentralWidget(central)
        _wbg = theme.CONSOLE_CHROME_BACKGROUND
        _wfg = theme.CONSOLE_TAB_TEXT
        self.setStyleSheet(
            f"QMainWindow {{ background-color: {_wbg}; }}"
            f"QMenuBar {{ background-color: {_wbg}; color: {_wfg}; spacing: 2px; }}"
            f"QMenuBar::item {{ padding: 4px 10px; background: transparent; }}"
            f"QMenuBar::item:selected {{ background-color: {theme.STUDIO_TOOLBAR_COMBO_BACKGROUND}; }}"
            f"QMenu {{ background-color: {theme.STUDIO_TOOLBAR_COMBO_BACKGROUND}; color: {_wfg}; }}"
            f"QMenu::item:selected {{ background-color: {theme.STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND}; }}"
            f"QStatusBar {{ background-color: {_wbg}; color: {_wfg}; border-top: 1px solid #555555; }}"
        )
        _ss_head = self._table_stylesheet_header_tables()
        _ss_body = self._table_stylesheet_body_tables()
        for _tw in (self._table_header_frozen, self._table_header):
            _tw.setStyleSheet(_ss_head)
        for _tw in (self._table_frozen, self._table):
            _tw.setStyleSheet(_ss_body)
        self._table.cellDoubleClicked.connect(self._on_parameter_scroll_table_double_clicked)
        self._table_frozen.cellDoubleClicked.connect(self._on_parameter_frozen_table_double_clicked)
        self._table.cellClicked.connect(self._on_parameter_scroll_table_clicked)
        self._table.horizontalHeader().sectionResized.connect(self._parawiz_on_body_column_resized)
        self._table.horizontalScrollBar().valueChanged.connect(self._parawiz_on_body_hscroll)
        self._table_header.horizontalScrollBar().valueChanged.connect(self._parawiz_on_header_hscroll)
        self._table.verticalScrollBar().valueChanged.connect(self._parawiz_on_body_vscroll)
        self._table_frozen.verticalScrollBar().valueChanged.connect(self._parawiz_on_frozen_vscroll)
        self._table.itemSelectionChanged.connect(self._parawiz_on_scroll_selection_changed)
        self._table_frozen.itemSelectionChanged.connect(self._parawiz_on_frozen_selection_changed)

        for _vp in (
            self._table.viewport(),
            self._table_header.viewport(),
            self._table_frozen.viewport(),
            self._table_header_frozen.viewport(),
        ):
            _vp.setMouseTracking(True)
            _vp.installEventFilter(self)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self.statusBar().showMessage("Ready")
        self._refresh_table()
        self.statusBar().showMessage(
            "Parameterquelle laden (Register DataSet Source oder Open Parameter Script). "
            "Danach: Doppelklick öffnet Skalar-Editor oder Kennlinie/Kennfeld; Strg+Klick auf zwei Kennfeld-Zellen startet den Vergleich.",
            12000,
        )
        # Preload HoloViews + calmapwidget after the first event-loop tick so startup stays responsive
        # but the first double-click on a parameter is not blocked by a multi-second HoloViews/calmap import.
        QTimer.singleShot(0, self._warm_calibration_plot_stack)

    def _warm_calibration_plot_stack(self) -> None:
        try:
            import synariustools.tools.calmapwidget  # noqa: F401
        except Exception:
            pass

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._parawiz_update_filter_field_width_one_third()
        if self._table.columnCount() > 0 and self._table_row.isVisible():
            self._parawiz_apply_param_table_total_width()
        else:
            self._parawiz_sync_param_scroll_geometry()

    @staticmethod
    def _parawiz_scroll_resize_hit_extended(tw: QTableWidget, vx: int) -> tuple[int | None, str]:
        """Erkennt Ziehpunkt zwischen Spalten ``(pair)`` oder rechte Kante der letzten Spalte ``(tail)``."""
        n = tw.columnCount()
        edge = MainWindow._PARAWIZ_COL_RESIZE_EDGE_PX
        if n <= 0:
            return None, "none"
        bx_r = tw.columnViewportPosition(n - 1) + tw.columnWidth(n - 1)
        if abs(vx - bx_r) <= edge:
            return n - 1, "tail"
        for i in range(n - 1):
            bx = tw.columnViewportPosition(i + 1)
            if abs(vx - bx) <= edge:
                return i, "pair"
        return None, "none"

    @staticmethod
    def _parawiz_frozen_resize_hit(tfr: QTableWidget, vx: int) -> bool:
        bx = tfr.columnViewportPosition(0) + tfr.columnWidth(0)
        return abs(vx - bx) <= MainWindow._PARAWIZ_COL_RESIZE_EDGE_PX

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        tw = self._table
        th = self._table_header
        tfr = self._table_frozen
        tfh = self._table_header_frozen
        min_w = 72
        et = event.type()
        scroll_vps = (tw.viewport(), th.viewport())
        frozen_vps = (tfr.viewport(), tfh.viewport())

        if watched in scroll_vps and tw.columnCount() > 0:
            tab = tw if watched is tw.viewport() else th
            if isinstance(event, QMouseEvent):
                if et == QEvent.Type.MouseMove:
                    if self._parawiz_resize_split_i is not None and not self._parawiz_resize_frozen_drag:
                        i = self._parawiz_resize_split_i
                        dx = int(event.globalPosition().x() - self._parawiz_resize_start_gx)
                        mode = self._parawiz_resize_scroll_mode
                        if mode == "tail":
                            new_w = max(min_w, self._parawiz_resize_w0 + dx)
                            tw.setColumnWidth(i, new_w)
                            th.setColumnWidth(i, new_w)
                        else:
                            new_left = self._parawiz_resize_w0 + dx
                            pair = self._parawiz_resize_w0 + self._parawiz_resize_w1
                            new_left = max(min_w, min(new_left, pair - min_w))
                            new_right = pair - new_left
                            tw.setColumnWidth(i, new_left)
                            th.setColumnWidth(i, new_left)
                            tw.setColumnWidth(i + 1, new_right)
                            th.setColumnWidth(i + 1, new_right)
                        self._parawiz_apply_param_table_total_width()
                        return True
                    pos = event.position().toPoint()
                    hit, _hm = MainWindow._parawiz_scroll_resize_hit_extended(tab, pos.x())
                    cur = Qt.CursorShape.SizeHorCursor if hit is not None else Qt.CursorShape.ArrowCursor
                    tw.viewport().setCursor(cur)
                    th.viewport().setCursor(cur)
                    return False
                if et == QEvent.Type.MouseButtonPress:
                    if event.button() != Qt.MouseButton.LeftButton:
                        return False
                    pos = event.position().toPoint()
                    hit, mode = MainWindow._parawiz_scroll_resize_hit_extended(tab, pos.x())
                    if hit is None or mode == "none":
                        return False
                    self._parawiz_resize_frozen_drag = False
                    self._parawiz_resize_split_i = hit
                    self._parawiz_resize_scroll_mode = mode
                    self._parawiz_resize_start_gx = int(event.globalPosition().x())
                    if mode == "tail":
                        self._parawiz_resize_w0 = tw.columnWidth(hit)
                        self._parawiz_resize_w1 = 0
                    else:
                        self._parawiz_resize_w0 = tw.columnWidth(hit)
                        self._parawiz_resize_w1 = tw.columnWidth(hit + 1)
                    (tw.viewport() if watched is tw.viewport() else th.viewport()).grabMouse()
                    return True
                if et == QEvent.Type.MouseButtonRelease:
                    if event.button() != Qt.MouseButton.LeftButton:
                        return False
                    if self._parawiz_resize_split_i is not None and not self._parawiz_resize_frozen_drag:
                        self._parawiz_resize_split_i = None
                        self._parawiz_resize_scroll_mode = None
                        tw.viewport().releaseMouse()
                        th.viewport().releaseMouse()
                        return True
            if (
                et == QEvent.Type.Leave
                and self._parawiz_resize_split_i is None
                and not self._parawiz_resize_frozen_drag
            ):
                tw.viewport().unsetCursor()
                th.viewport().unsetCursor()
            return False

        if watched in frozen_vps and tfr.columnCount() > 0 and tw.columnCount() > 0:
            if isinstance(event, QMouseEvent):
                if et == QEvent.Type.MouseMove:
                    if self._parawiz_resize_frozen_drag:
                        dx = int(event.globalPosition().x() - self._parawiz_resize_start_gx)
                        pair = self._parawiz_resize_w0 + self._parawiz_resize_w1
                        new_f = self._parawiz_resize_w0 + dx
                        new_f = max(min_w, min(new_f, pair - min_w))
                        new_s0 = pair - new_f
                        tfr.setColumnWidth(0, new_f)
                        tfh.setColumnWidth(0, new_f)
                        tw.setColumnWidth(0, new_s0)
                        th.setColumnWidth(0, new_s0)
                        self._parawiz_apply_param_table_total_width()
                        return True
                    pos = event.position().toPoint()
                    hit_f = self._parawiz_frozen_resize_hit(tfr, pos.x())
                    cur = Qt.CursorShape.SizeHorCursor if hit_f else Qt.CursorShape.ArrowCursor
                    tfr.viewport().setCursor(cur)
                    tfh.viewport().setCursor(cur)
                    return False
                if et == QEvent.Type.MouseButtonPress:
                    if event.button() != Qt.MouseButton.LeftButton:
                        return False
                    pos = event.position().toPoint()
                    if not self._parawiz_frozen_resize_hit(tfr, pos.x()):
                        return False
                    self._parawiz_resize_frozen_drag = True
                    self._parawiz_resize_split_i = None
                    self._parawiz_resize_start_gx = int(event.globalPosition().x())
                    self._parawiz_resize_w0 = tfr.columnWidth(0)
                    self._parawiz_resize_w1 = tw.columnWidth(0)
                    tfr.viewport().grabMouse()
                    return True
                if et == QEvent.Type.MouseButtonRelease:
                    if event.button() != Qt.MouseButton.LeftButton:
                        return False
                    if self._parawiz_resize_frozen_drag:
                        self._parawiz_resize_frozen_drag = False
                        tfr.viewport().releaseMouse()
                        return True
            if et == QEvent.Type.Leave and not self._parawiz_resize_frozen_drag:
                tfr.viewport().unsetCursor()
                tfh.viewport().unsetCursor()
            return False

        return super().eventFilter(watched, event)

    def _parawiz_update_filter_field_width_one_third(self) -> None:
        """Suchfeld ≈ ein Drittel der Breite des Zentralbereichs (min. 180 px)."""
        cw = self.centralWidget()
        outer = max(1, int(cw.width())) if cw is not None and cw.width() > 0 else max(1, int(self.width()))
        self._filter_name.setFixedWidth(max(180, outer // 3))

    @classmethod
    def _parawiz_missing_dataset_brush(cls) -> QBrush:
        """Graue Kreuzschraffur (beide Diagonalen gleich) für fehlende Satz-Spalten."""
        if cls._parawiz_missing_ds_brush_cached is None:
            pm = QPixmap(12, 12)
            pm.fill(QColor("#d2d2d2"))
            with QPainter(pm) as p:
                p.setPen(QPen(QColor("#6a6a6a"), 1))
                p.drawLine(0, 12, 12, 0)
                p.drawLine(0, 0, 12, 12)
            cls._parawiz_missing_ds_brush_cached = QBrush(pm)
        return cls._parawiz_missing_ds_brush_cached

    @staticmethod
    def _parawiz_breeze_icons_dir() -> Path:
        return Path(_synarius_parawiz_pkg.__file__).resolve().parent / "icons" / "breeze"

    def _parawiz_set_breeze_action_icons(self) -> None:
        """Toolbar-/Menü-Icons (KDE Breeze, siehe icons/breeze/BREEZE_ICONS_NOTICE.txt)."""
        d = self._parawiz_breeze_icons_dir()
        fg = QColor(theme.STUDIO_TOOLBAR_FOREGROUND)
        mapping: list[tuple[QAction, str]] = [
            (self._act_open_script, "document-open.svg"),
            (self._act_open_source, "document-import.svg"),
            (self._act_refresh, "view-refresh.svg"),
            (self._act_console, "utilities-terminal.svg"),
        ]
        for act, name in mapping:
            p = d / name
            if p.is_file():
                act.setIcon(icon_from_tinted_svg_file(p, fg, logical_side=22))

    def _parawiz_update_param_table_area_visibility(self) -> None:
        show = len(self._cached_datasets) > 0
        self._table_row.setVisible(show)
        self._filter_row.setVisible(show)
        if show:
            self._parawiz_update_filter_field_width_one_third()
            self._parawiz_update_filter_count_label()

    def _parawiz_sync_param_scroll_geometry(self) -> None:
        if not hasattr(self, "_param_table_scroll"):
            return
        vp = self._param_table_scroll.viewport()
        if vp is None:
            return
        w = self._param_table_host.width()
        if w <= 0:
            return
        vp_h = int(vp.height())
        scroll_h = int(self._param_table_scroll.height())
        h = vp_h
        # Vor dem ersten Layout ist die Viewport-Höhe oft 0–2 px — setFixedSize würde die Tabelle dauerhaft quetschen.
        if h < 80:
            if scroll_h >= 120:
                h = max(scroll_h - 8, 200)
            else:
                QTimer.singleShot(0, self._parawiz_sync_param_scroll_geometry)
                return
        self._param_table_host.setFixedSize(w, h)

    def _parawiz_on_body_column_resized(self, logical_index: int, old_size: int, new_size: int) -> None:
        _ = old_size
        if self._parawiz_header_scroll_guard:
            return
        self._parawiz_header_scroll_guard = True
        try:
            self._table_header.setColumnWidth(logical_index, new_size)
            self._parawiz_apply_param_table_total_width()
        finally:
            self._parawiz_header_scroll_guard = False

    def _parawiz_on_body_hscroll(self, value: int) -> None:
        if self._parawiz_header_scroll_guard:
            return
        self._parawiz_header_scroll_guard = True
        try:
            self._table_header.horizontalScrollBar().setValue(value)
        finally:
            self._parawiz_header_scroll_guard = False

    def _parawiz_on_header_hscroll(self, value: int) -> None:
        if self._parawiz_header_scroll_guard:
            return
        self._parawiz_header_scroll_guard = True
        try:
            self._table.horizontalScrollBar().setValue(value)
        finally:
            self._parawiz_header_scroll_guard = False

    def _parawiz_on_body_vscroll(self, value: int) -> None:
        if self._parawiz_vscroll_guard:
            return
        self._parawiz_vscroll_guard = True
        try:
            self._table_frozen.verticalScrollBar().setValue(value)
        finally:
            self._parawiz_vscroll_guard = False

    def _parawiz_on_frozen_vscroll(self, value: int) -> None:
        if self._parawiz_vscroll_guard:
            return
        self._parawiz_vscroll_guard = True
        try:
            self._table.verticalScrollBar().setValue(value)
        finally:
            self._parawiz_vscroll_guard = False

    def _parawiz_on_scroll_selection_changed(self) -> None:
        if self._parawiz_sel_guard:
            return
        sm = self._table.selectionModel()
        if sm is None:
            return
        rows = sm.selectedRows()
        if not rows:
            return
        self._parawiz_sel_guard = True
        try:
            self._table_frozen.selectRow(rows[0].row())
        finally:
            self._parawiz_sel_guard = False

    def _parawiz_on_frozen_selection_changed(self) -> None:
        if self._parawiz_sel_guard:
            return
        sm = self._table_frozen.selectionModel()
        if sm is None:
            return
        rows = sm.selectedRows()
        if not rows:
            return
        self._parawiz_sel_guard = True
        try:
            self._table.selectRow(rows[0].row())
        finally:
            self._parawiz_sel_guard = False

    def _parawiz_apply_param_table_total_width(self) -> None:
        min_w = 72
        wf = max(self._table_frozen.columnWidth(0), self._table_header_frozen.columnWidth(0), min_w)
        self._table_frozen.setColumnWidth(0, wf)
        self._table_header_frozen.setColumnWidth(0, wf)
        self._frozen_block.setFixedWidth(wf + 2)

        tw = self._table
        n = tw.columnCount()
        extra = 6
        if n <= 0:
            self._param_table_host.setFixedWidth(0)
            self._param_table_scroll.setVisible(False)
        else:
            self._param_table_scroll.setVisible(True)
            cols_sum = sum(tw.columnWidth(c) for c in range(n))
            # Vertikale Scrollbar verengt die Viewport-Breite — sonst erscheint ein horizontaler Scrollbalken.
            vs_extra = 0
            if tw.rowCount() > 0:
                vs_extra = max(int(tw.verticalScrollBar().sizeHint().width()), 14)
            total = cols_sum + extra + vs_extra
            th = self._table_header
            vp_w_raw = int(self._param_table_scroll.viewport().width())
            vp_ready = vp_w_raw >= MainWindow._PARAWIZ_SCROLL_VP_WIDTH_READY_MIN
            # Kein Auffüllen bis zur Viewport-Breite: Spalten bleiben nur so breit wie der Inhalt
            # (plus Mindestbreiten in _parawiz_uniform_column_widths); rechts bleibt ggf. Leerraum.
            vp_cap = vp_w_raw if vp_w_raw > 0 else total
            total_before_shrink = total
            resizing = self._parawiz_resize_split_i is not None or self._parawiz_resize_frozen_drag
            inner_budget = vp_cap - extra - vs_extra
            if vp_ready and inner_budget > 0 and not resizing:
                cols_sum = sum(tw.columnWidth(c) for c in range(n))
                if cols_sum > inner_budget and inner_budget >= n * min_w:
                    target = inner_budget
                    val_cols = [c for c in range(n) if c % 2 == 1]
                    if not val_cols:
                        val_cols = list(range(n))
                    base = [
                        max(min_w, int(round(tw.columnWidth(c) * (target / float(cols_sum)))))
                        for c in range(n)
                    ]
                    rem = target - sum(base)
                    vi = 0
                    while rem > 0 and val_cols:
                        c = val_cols[vi % len(val_cols)]
                        base[c] += 1
                        rem -= 1
                        vi += 1
                    while sum(base) > target:
                        cmax = max(range(n), key=lambda c: base[c])
                        if base[cmax] <= min_w:
                            break
                        base[cmax] -= 1
                    for c in range(n):
                        tw.setColumnWidth(c, base[c])
                        th.setColumnWidth(c, base[c])
                    cols_sum = sum(base)
                    total = cols_sum + extra + vs_extra
                elif cols_sum > inner_budget and inner_budget < n * min_w:
                    pass
            cols_sum = sum(tw.columnWidth(c) for c in range(n))
            total = cols_sum + extra + vs_extra
            # Explizite Breiten: QTableViewport = Widgetbreite − VScroll; bei Policy Fixed war oft zu schmal.
            th.setFixedWidth(int(cols_sum))
            tw.setFixedWidth(int(cols_sum + vs_extra))
            # Host immer mindestens so breit wie Inhalt — niemals min(total,vp) (clippt Spalten).
            self._param_table_host.setFixedWidth(max(int(total), 1))
        self._parawiz_sync_param_scroll_geometry()

    def _parawiz_uniform_column_widths(self) -> None:
        """Spalten nach Inhalt; Namensspalte fix; Rest scrollt horizontal gemeinsam."""
        self._parawiz_header_scroll_guard = True
        try:
            self._table_frozen.resizeColumnsToContents()
            self._table_header_frozen.resizeColumnsToContents()
            min_w = 72
            wf = max(self._table_frozen.columnWidth(0), self._table_header_frozen.columnWidth(0), min_w)
            self._table_frozen.setColumnWidth(0, wf)
            self._table_header_frozen.setColumnWidth(0, wf)

            tw = self._table
            th = self._table_header
            n = tw.columnCount()
            if n > 0:
                tw.resizeColumnsToContents()
                # Kein th.resizeColumnsToContents(): bei Zeile 0 mit setSpan(…,1,2)
                # liefert Qt hier oft falsche Breiten — z. B. kollabiert Spalte „Value / Shape“
                # des zweiten Satzes, obwohl die Datenzeilen existieren.
                for c in range(n):
                    w = max(tw.columnWidth(c), min_w)
                    tw.setColumnWidth(c, w)
                    th.setColumnWidth(c, w)
                datasets = self._cached_datasets
                fm = th.fontMetrics()
                for i in range(len(datasets)):
                    ps_name, _ = datasets[i]
                    c0 = 2 * i
                    c1 = c0 + 1
                    if c1 >= n:
                        break
                    need = fm.horizontalAdvance(str(ps_name)) + 28
                    cur = tw.columnWidth(c0) + tw.columnWidth(c1)
                    if cur < need:
                        delta = need - cur
                        tw.setColumnWidth(c1, tw.columnWidth(c1) + delta)
                        th.setColumnWidth(c1, th.columnWidth(c1) + delta)
            self._parawiz_compact_body_row_heights()
            self._parawiz_apply_param_table_total_width()
        finally:
            self._parawiz_header_scroll_guard = False

    def _parawiz_compact_body_row_heights(self) -> None:
        """Nur Daten-Tabellen: niedrigere Zeilenhöhe (Kopfzeilen-Widgets unverändert)."""
        tw = self._table
        tfr = self._table_frozen
        fm = tw.fontMetrics()
        rh = max(16, fm.height() + 1)
        for r in range(tw.rowCount()):
            tw.setRowHeight(r, rh)
        for r in range(tfr.rowCount()):
            tfr.setRowHeight(r, rh)

    def _parawiz_fit_window_for_param_table_horizontal(self) -> None:
        """Fenster so weit verbreitern, dass Name + alle Spalten ohne horizontalen Scroll sichtbar sind."""
        cw = self.centralWidget()
        if cw is None:
            return
        need = self._frozen_block.width() + self._param_table_host.width() + 48
        if need <= 0:
            return
        avail = cw.width()
        if need > avail:
            self.resize(self.width() + (need - avail), self.height())

    def _parawiz_reapply_param_table_host_width_after_layout(self) -> None:
        """Nach sichtbarer vertikaler Scrollbar Hostbreite erneut setzen (kein horizontaler Scroll im TableWidget)."""
        if self._table.columnCount() <= 0:
            return
        self._parawiz_apply_param_table_total_width()
        self._parawiz_sync_param_scroll_geometry()
        self._parawiz_fit_window_for_param_table_horizontal()

    def _parawiz_reset_param_table_hscroll(self) -> None:
        self._parawiz_header_scroll_guard = True
        try:
            self._table.horizontalScrollBar().setValue(0)
            self._table_header.horizontalScrollBar().setValue(0)
        finally:
            self._parawiz_header_scroll_guard = False

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

    def _parawiz_patch_cached_scalar_value_label(self, pid: UUID, value_label: str) -> None:
        """Hält ``_cached_rows`` konsistent, falls nur ein Skalar geändert wurde (z. B. Filter-Rebuild)."""
        pid_u = pid
        new_list: list[tuple[str, dict[UUID, tuple[str, str, UUID]]]] = []
        for name, by_ds in self._cached_rows:
            if not any(len(t) >= 3 and t[2] == pid_u for t in by_ds.values()):
                new_list.append((name, by_ds))
                continue
            new_by: dict[UUID, tuple[str, str, UUID]] = {}
            for ds_id, t in by_ds.items():
                if len(t) >= 3 and t[2] == pid_u:
                    new_by[ds_id] = (t[0], value_label, t[2])
                else:
                    new_by[ds_id] = t
            new_list.append((name, new_by))
        self._cached_rows = new_list

    @staticmethod
    def _parawiz_item_parameter_id_matches(item: QTableWidgetItem | None, pid: UUID) -> bool:
        """Vergleicht ``UserRole`` der Zelle mit ``pid`` (str/UUID/QVariant-sicher)."""
        if item is None:
            return False
        raw = item.data(Qt.ItemDataRole.UserRole)
        if raw is None:
            return False
        if isinstance(raw, UUID):
            return raw == pid
        try:
            return UUID(str(raw)) == pid
        except (ValueError, TypeError):
            return False

    def _parawiz_patch_table_value_cells_for_parameter_id(self, pid: UUID, *, value_text: str) -> None:
        """Alle Value-Zellen für ``pid`` setzen (Spalten 1,3,5,…; wie ``get_parameter_table_summary`` für Skalare)."""
        tw = self._table
        ncol = tw.columnCount()
        updated = False
        for r in range(tw.rowCount()):
            for c in range(1, ncol, 2):
                it = tw.item(r, c)
                if not MainWindow._parawiz_item_parameter_id_matches(it, pid):
                    continue
                it.setText(value_text)
                updated = True
        if updated:
            tw.viewport().update()

    def _parawiz_push_calibration_to_model(self, viewer: object, pid: UUID) -> None:
        """Nach Apply: Werte wie per CCP ins Modell schreiben und in der CLI-Konsole protokollieren."""
        from synariustools.tools.calmapwidget.widget import CalibrationMapWidget

        if not isinstance(viewer, CalibrationMapWidget):
            return
        vals, axes_map = viewer.applied_values_and_axes()
        vals = np.asarray(vals, dtype=np.float64)
        self._parawiz_write_calibration_numeric_to_model(pid, viewer._data.title, vals, axes_map)

    def _parawiz_write_calibration_numeric_to_model(
        self,
        pid: UUID,
        title: str,
        vals: np.ndarray,
        axes_map: dict[int, np.ndarray],
    ) -> None:
        """CCP + Repository-Fallback; Tabelle aktualisieren."""
        node = self._controller.model.find_by_id(pid)
        if not isinstance(node, ComplexInstance):
            self.statusBar().showMessage("ParaWiz: Kenngröße nicht im Modell (UUID).", 8000)
            return
        hr = node.hash_name
        vals = np.asarray(vals, dtype=np.float64)
        repo = self._controller.model.parameter_runtime().repo

        def _log(cmd: str, ok: str | None = None, err: str | None = None) -> None:
            cw = self._console_window
            if cw is not None:
                cw.append_parawiz_ccp(cmd, ok, err)

        finite = bool(np.isfinite(vals).all()) and all(
            bool(np.isfinite(np.asarray(a, dtype=np.float64)).all()) for a in axes_map.values()
        )

        def _value_literal(v: np.ndarray) -> str:
            v = np.asarray(v, dtype=np.float64)
            if v.ndim == 0:
                return repr(float(v.item()))
            return json.dumps(v.tolist(), separators=(",", ":"))

        value_cmd = f"set {hr}.value {_value_literal(vals)}"
        axis_cmds: list[str] = []
        for axis_idx in range(int(vals.ndim)):
            arr = axes_map.get(axis_idx)
            if arr is None:
                continue
            a = np.asarray(arr, dtype=np.float64).reshape(-1)
            lit = json.dumps(a.tolist(), separators=(",", ":"))
            axis_cmds.append(f"set {hr}.x{axis_idx + 1}_axis {lit}")

        cmds = [value_cmd] + axis_cmds
        total_len = sum(len(c) for c in cmds)
        allow_ccp = finite and total_len <= self._PARAWIZ_CCP_CMD_MAX_TOTAL

        wrote_via_ccp = False
        if allow_ccp:
            try:
                for cmd in cmds:
                    out = self._controller.execute(cmd)
                    _log(cmd, out if out else "ok", None)
                wrote_via_ccp = True
            except CommandError as exc:
                err = str(exc)
                for cmd in cmds:
                    _log(cmd, None, err)

        if not wrote_via_ccp:
            try:
                repo.set_value(pid, vals)
                for axis_idx in sorted(axes_map):
                    if 0 <= axis_idx < int(vals.ndim):
                        repo.set_axis_values(pid, axis_idx, axes_map[axis_idx])
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "ParaWiz",
                    f"Die Kenngröße konnte nicht ins Modell geschrieben werden:\n{exc}",
                )
                return
            if allow_ccp:
                _log(f"# ParaWiz: CCP fehlgeschlagen — gleiche Daten per Repository geschrieben ({title})", "ok", None)
            else:
                why = "Werte nicht endlich" if not finite else f"CCP-Gesamtlänge {total_len}"
                _log(
                    f"# ParaWiz: Direktschreibung ({why}); shape={tuple(vals.shape)} ref={hr} ({title})",
                    "ok",
                    None,
                )

        # CCP aktualisiert zwar die virtuellen Attribute (→ Repo), die ParaWiz-Tabelle liest aber
        # direkt aus DuckDB — explizit spiegeln, damit get_parameter_table_summary nach Refresh stimmt.
        if wrote_via_ccp:
            try:
                repo.set_value(pid, vals)
                for axis_idx in sorted(axes_map):
                    if 0 <= axis_idx < int(vals.ndim):
                        repo.set_axis_values(pid, axis_idx, axes_map[axis_idx])
            except Exception:
                pass

        self._refresh_table()
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        vals_f = np.asarray(vals, dtype=np.float64)
        if vals_f.ndim == 0:
            scalar_label = repr(float(vals_f.item()))
            self._parawiz_patch_cached_scalar_value_label(pid, scalar_label)
            self._parawiz_patch_table_value_cells_for_parameter_id(pid, value_text=scalar_label)
            # Nach Layout/Timer aus _populate_table_rows erneut setzen (Qt kann Zellen verzögert finalisieren).
            QTimer.singleShot(
                0,
                lambda p=pid, lbl=scalar_label: self._parawiz_patch_table_value_cells_for_parameter_id(
                    p, value_text=lbl
                ),
            )
        if app is not None:
            app.processEvents()
        self.statusBar().showMessage(f"Modell übernommen: {title}", 6000)

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

    def _table_stylesheet_common(self) -> str:
        return (
            "QTableWidget {"
            f" background-color: {theme.RESOURCES_PANEL_BACKGROUND};"
            f" alternate-background-color: {theme.RESOURCES_PANEL_ALTERNATE_ROW};"
            " color: #1a1a1a;"
            " gridline-color: transparent;"
            " border: none;"
            " font-size: 11px;"
            "}"
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
            "QAbstractScrollArea::corner { background-color: #2f2f2f; border: none; }"
        )

    def _table_stylesheet_header_tables(self) -> str:
        """Zwei Titelzeilen (Kopf) — etwas luftigeres Item-Padding."""
        return self._table_stylesheet_common() + "QTableWidget::item { padding: 2px 4px; }"

    def _table_stylesheet_body_tables(self) -> str:
        """Parameterliste (Daten) — weniger Abstand oben/unten im Zelltext."""
        return self._table_stylesheet_common() + "QTableWidget::item { padding: 0px 2px; margin: 0px; }"

    def _create_actions(self) -> None:
        self._act_open_script = QAction("Open Parameter Script...", self)
        self._act_open_script.triggered.connect(self._open_script)
        self._act_open_source = QAction("Register DataSet Source...", self)
        self._act_open_source.triggered.connect(self._register_data_set_source)
        self._act_refresh = QAction("Refresh", self)
        self._act_refresh.triggered.connect(self._refresh_table)
        self._act_console = QAction("CLI Console", self)
        self._act_console.triggered.connect(self._open_console)
        self._parawiz_set_breeze_action_icons()

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

    @staticmethod
    def _syn_script_dcm_import_meta(script_path: Path) -> tuple[bool, int]:
        """True if the script contains ``import dcm`` lines; byte sum of resolvable DCM paths (for progress UI)."""
        import shlex

        has_import = False
        total_bytes = 0
        try:
            text = script_path.read_text(encoding="utf-8")
        except OSError:
            return (False, 0)
        base = script_path.resolve().parent
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                parts = shlex.split(s)
            except ValueError:
                continue
            if len(parts) < 3 or parts[0] != "import" or parts[1] != "dcm":
                continue
            has_import = True
            raw = parts[2]
            p = Path(raw).expanduser()
            if not p.is_file():
                alt = (base / raw).expanduser()
                if alt.is_file():
                    p = alt
            if p.is_file():
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass
        return (has_import, total_bytes)

    def _open_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open parameter script",
            "",
            "Synarius scripts (*.syn *.txt *.cli);;All files (*)",
        )
        if not path:
            return
        path_obj = Path(path)
        cli_path = path.replace("\\", "/")
        had_dcm_import, dcm_bytes = self._syn_script_dcm_import_meta(path_obj)
        specs_accum = [0]

        def _phase(ph: str, n: int) -> None:
            if ph == "write":
                specs_accum[0] += n
            self._dcm_import_phase(ph, n)

        ctl = self._controller
        dcm_table_gui = False
        try:
            try:
                if had_dcm_import:
                    self._dcm_import_file_bytes = dcm_bytes
                    ctl.dcm_import_progress_hook = self._dcm_import_progress
                    ctl.dcm_import_phase_hook = _phase
                    ctl.dcm_import_cooperative_hook = self._dcm_cooperative_ui
                self._controller.execute(f'load "{cli_path}"')
                dcm_table_gui = (
                    had_dcm_import
                    and self._dcm_import_write_total >= self._DCM_IMPORT_PROGRESS_MIN_SPECS
                    and self._dcm_import_status_frame_in_bar
                )
            finally:
                self._dcm_import_file_bytes = 0
                ctl.dcm_import_progress_hook = None
                ctl.dcm_import_phase_hook = None
                ctl.dcm_import_cooperative_hook = None
                if not dcm_table_gui and had_dcm_import:
                    self._dcm_import_remove_progress_bar()
                    self._dcm_import_write_total = 0

            if dcm_table_gui:
                try:
                    self._refresh_table(
                        dcm_table_progress=True,
                        dcm_imported_hint=max(1, specs_accum[0]),
                    )
                finally:
                    self._dcm_import_remove_progress_bar()
                    self._dcm_import_write_total = 0
            else:
                self._refresh_table()
            self.statusBar().showMessage(f"Loaded script: {path_obj.name}", 6000)
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

    @staticmethod
    def _parawiz_float_payload_bytes(a: np.ndarray) -> bytes:
        """Robuster Zahlenvergleich: Rundung + kanonisches Vorzeichen von 0 (Float-Noise aus I/O)."""
        x = np.asarray(a, dtype=np.float64).reshape(-1)
        if x.size == 0:
            return b""
        y = np.round(x, 10)
        y = np.where(np.abs(y) < 1e-15, 0.0, y)
        return y.tobytes()

    @staticmethod
    def _parawiz_record_va_fingerprint(rec: ParameterRecord) -> tuple:
        """Werte + Achsenwerte (ohne Achsnamen/-einheiten — die kommen oft unterschiedlich aus dem DCM)."""
        if rec.is_text:
            return ("t", str(rec.category), str(rec.text_value))
        v = np.asarray(rec.values, dtype=np.float64)
        ax_parts: list[tuple[int, bytes]] = []
        for idx in sorted(rec.axes.keys()):
            ax_parts.append((int(idx), MainWindow._parawiz_float_payload_bytes(rec.axes[idx])))
        return ("n", v.shape, MainWindow._parawiz_float_payload_bytes(v), tuple(ax_parts))

    @staticmethod
    def _parawiz_record_meta_fingerprint(rec: ParameterRecord) -> tuple:
        """Nur inhaltlich relevante Metadaten (kein Anzeige-/DCM-Kram wie LANGNAME, EINHEIT, Kommentar)."""
        if rec.is_text:
            return (str(rec.category).upper(),)
        return (
            str(rec.category).upper(),
            str(rec.numeric_format),
            str(rec.value_semantics),
        )

    @staticmethod
    def _parawiz_record_full_fingerprint(rec: ParameterRecord) -> tuple:
        return (MainWindow._parawiz_record_va_fingerprint(rec), MainWindow._parawiz_record_meta_fingerprint(rec))

    @staticmethod
    def _parawiz_cross_dataset_style_for_row(
        repo: ParametersRepository,
        by_ds: dict[UUID, tuple[str, str, UUID]],
        datasets: list[tuple[str, UUID]],
    ) -> _ParawizRowCrossDsStyle:
        if len(datasets) < 2:
            return _PARAWIZ_ROW_STYLE_NEUTRAL
        present: list[tuple[int, ParameterRecord]] = []
        for i, (_, ds_id) in enumerate(datasets):
            hit = by_ds.get(ds_id)
            if hit is None:
                continue
            pid = hit[2]
            try:
                rec = repo.get_record(pid)
            except Exception:
                continue
            present.append((i, rec))
        if len(present) < 2:
            return _PARAWIZ_ROW_STYLE_NEUTRAL

        va_fps = [MainWindow._parawiz_record_va_fingerprint(r) for _, r in present]
        meta_fps = [MainWindow._parawiz_record_meta_fingerprint(r) for _, r in present]
        va_unique = list(dict.fromkeys(va_fps))
        meta_unique = list(dict.fromkeys(meta_fps))
        va_unique_count = len(va_unique)
        meta_unique_count = len(meta_unique)
        values_differ = va_unique_count > 1
        meta_differ = meta_unique_count > 1
        # Stern nur für "Werte/Achsen gleich, aber Metadaten verschieden".
        star = (not values_differ) and meta_differ

        if (not values_differ) and (not meta_differ):
            return _ParawizRowCrossDsStyle(False, False, {}, None)

        if values_differ:
            # Werte/Achsen unterschiedlich: fett + Clusterfarben in Datensatzspalten,
            # Name-Spalte nur fett (kein Farbcode), damit "gleich/ungleich" klar bleibt.
            cid = {fp: idx for idx, fp in enumerate(va_unique)}
            fps_for_color = va_fps
            row_bold = True
            frozen_fg = None
        else:
            # Nur Metadaten unterschiedlich: farbig, aber nicht fett.
            cid = {fp: idx for idx, fp in enumerate(meta_unique)}
            fps_for_color = meta_fps
            row_bold = False
            frozen_fg = QColor(_PARAWIZ_NAME_COL_MIXED_HEX)
        palette = _PARAWIZ_DIFF_CLUSTER_HEX
        col_fg: dict[int, QColor] = {}
        for (i, _), fp in zip(present, fps_for_color):
            idx = cid[fp]
            col_fg[i] = QColor(palette[idx % len(palette)])
        return _ParawizRowCrossDsStyle(row_bold, star, col_fg, frozen_fg)

    @staticmethod
    def _parawiz_row_present_dataset_count(
        by_ds: dict[UUID, tuple[str, str, UUID]],
        datasets: list[tuple[str, UUID]],
    ) -> int:
        return sum(1 for _dsn, ds_id in datasets if by_ds.get(ds_id) is not None)

    def _parawiz_row_passes_cross_dataset_filters(
        self,
        name: str,
        by_ds: dict[UUID, tuple[str, str, UUID]],
        datasets: list[tuple[str, UUID]],
    ) -> bool:
        hide_unequal = self._btn_filter_hide_unequal.isChecked()
        hide_equal = self._btn_filter_hide_equal.isChecked()
        if not hide_unequal and not hide_equal:
            return True
        st = self._cached_row_styles.get(name, _PARAWIZ_ROW_STYLE_NEUTRAL)
        present_n = MainWindow._parawiz_row_present_dataset_count(by_ds, datasets)
        comparable = len(datasets) >= 2 and present_n >= 2
        if hide_unequal and comparable and st.row_bold:
            return False
        if hide_equal and (not comparable or not st.row_bold):
            return False
        return True

    def _parawiz_filtered_rows_list(
        self,
    ) -> list[tuple[str, dict[UUID, tuple[str, str, UUID]]]]:
        rows = list(self._cached_rows)
        flt = self._filter_name.text().strip()
        if flt:
            rows = [row for row in rows if _parameter_name_matches_filter(row[0], flt)]
        ds = self._cached_datasets
        if self._btn_filter_hide_unequal.isChecked() or self._btn_filter_hide_equal.isChecked():
            rows = [row for row in rows if self._parawiz_row_passes_cross_dataset_filters(row[0], row[1], ds)]
        return rows

    def _on_cross_dataset_filter_toggled(self, _checked: bool) -> None:
        self._populate_table_rows()

    def _parawiz_sync_cross_dataset_filter_buttons(self) -> None:
        ok = len(self._cached_datasets) >= 2
        for b in (self._btn_filter_hide_unequal, self._btn_filter_hide_equal):
            b.setEnabled(ok)
        if not ok:
            self._btn_filter_hide_unequal.blockSignals(True)
            self._btn_filter_hide_equal.blockSignals(True)
            self._btn_filter_hide_unequal.setChecked(False)
            self._btn_filter_hide_equal.setChecked(False)
            self._btn_filter_hide_unequal.blockSignals(False)
            self._btn_filter_hide_equal.blockSignals(False)

    def _parawiz_update_filter_count_label(self) -> None:
        total = len(self._cached_rows)
        shown = len(self._parawiz_filtered_rows_list())
        self._filter_count_label.setText(f"{shown}/{total} Parameter")

    def _collect_rows(
        self,
        *,
        on_seen: Callable[[int], None] | None = None,
        seen_every: int = 100,
    ) -> tuple[
        list[tuple[str, UUID]],
        list[tuple[str, dict[UUID, tuple[str, str, UUID]]]],
        dict[str, _ParawizRowCrossDsStyle],
    ]:
        rows_by_name: dict[str, dict[UUID, tuple[str, str, UUID]]] = {}
        model = self._controller.model
        repo = model.parameter_runtime().repo
        data_sets_root = model.parameter_runtime().data_sets_root()
        datasets: list[tuple[str, UUID]] = []
        for ds_node in data_sets_root.children:
            if not isinstance(ds_node, ComplexInstance):
                continue
            if model.is_in_trash_subtree(ds_node) or ds_node.id is None:
                continue
            # Cal-Parameter liegen unter data_sets neben den DataSet-Knoten; nur PARAMETER_DATA_SET zählen.
            try:
                if str(ds_node.get("type")) != "MODEL.PARAMETER_DATA_SET":
                    continue
            except KeyError:
                continue
            datasets.append((repo.get_dataset_init_file_stem(ds_node.id), ds_node.id))
        ds_ids = {ds_id for _, ds_id in datasets}

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
            ds_id: UUID | None = None
            try:
                ds_raw = node.get("data_set_id")
                if ds_raw:
                    ds_id = UUID(str(ds_raw))
            except Exception:
                ds_id = None
            if ds_id is None:
                try:
                    rec = repo.get_record(node.id)
                    ds_id = rec.data_set_id
                except Exception:
                    continue
            if ds_id not in ds_ids:
                ds_name = repo.get_dataset_init_file_stem(ds_id)
                datasets.append((ds_name, ds_id))
                ds_ids.add(ds_id)
            ds_map = rows_by_name.setdefault(summary.name, {})
            ds_map[ds_id] = (summary.category, summary.value_label, node.id)
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
        rows = [(name, rows_by_name[name]) for name in sorted(rows_by_name, key=str.lower)]
        datasets.sort(key=lambda t: (str(t[0]).lower(), str(t[1])))
        row_styles: dict[str, _ParawizRowCrossDsStyle] = {}
        if len(datasets) < 2:
            for name, _bd in rows:
                row_styles[name] = _PARAWIZ_ROW_STYLE_NEUTRAL
        else:
            for name, by_ds in rows:
                row_styles[name] = MainWindow._parawiz_cross_dataset_style_for_row(repo, by_ds, datasets)
        return datasets, rows, row_styles

    def _apply_filter_to_table(self) -> None:
        self._populate_table_rows()

    @staticmethod
    def _parawiz_header_banner_item(text: str, bg_hex: str) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
        it.setBackground(QBrush(QColor(bg_hex)))
        it.setForeground(QBrush(QColor("#ffffff")))
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return it

    def _parawiz_install_frozen_header(self) -> None:
        """Linke Kopfspalte: Parameter / Name (scrollt nicht horizontal)."""
        t = self._table_header_frozen
        if hasattr(t, "clearSpans"):
            t.clearSpans()
        t.setColumnCount(1)
        t.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
        t.setItem(0, 0, self._parawiz_header_banner_item("Parameter", "#525252"))
        t.setItem(1, 0, self._parawiz_header_banner_item("Name", "#525252"))
        t.resizeRowsToContents()

    def _parawiz_install_scroll_headers(self, datasets: list[tuple[str, UUID]]) -> None:
        """Rechte Kopfzeilen: je Parameter-Satz Type+Value (scrollen mit dem Körper)."""
        t = self._table_header
        if hasattr(t, "clearSpans"):
            t.clearSpans()
        cc_rest = 2 * len(datasets)
        t.setColumnCount(cc_rest)
        if cc_rest == 0:
            t.setRowCount(0)
            t.setFixedHeight(0)
            return
        t.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
        for i, (ps_name, _) in enumerate(datasets):
            c0 = 2 * i
            color = self._DATASET_HEADER_COLORS[i % len(self._DATASET_HEADER_COLORS)]
            t.setItem(0, c0, self._parawiz_header_banner_item(ps_name, color))
            t.setSpan(0, c0, 1, 2)
        for i in range(len(datasets)):
            c0 = 2 * i
            c1 = c0 + 1
            color = self._DATASET_HEADER_COLORS[i % len(self._DATASET_HEADER_COLORS)]
            t.setItem(1, c0, self._parawiz_header_banner_item("Type", color))
            t.setItem(1, c1, self._parawiz_header_banner_item("Value / Shape", color))
        t.resizeRowsToContents()

    def _parawiz_unify_param_header_heights(self) -> None:
        hf = sum(self._table_header_frozen.rowHeight(r) for r in range(MainWindow.PARAWIZ_TABLE_HEADER_ROWS))
        self._table_header_frozen.setFixedHeight(max(hf, 40))
        if self._table_header.columnCount() > 0 and self._table_header.rowCount() >= MainWindow.PARAWIZ_TABLE_HEADER_ROWS:
            hs = sum(self._table_header.rowHeight(r) for r in range(MainWindow.PARAWIZ_TABLE_HEADER_ROWS))
            h = max(hf, hs, 40)
            self._table_header_frozen.setFixedHeight(h)
            self._table_header.setFixedHeight(h)

    def _populate_table_rows(
        self,
        *,
        dcm_table_progress: bool = False,
        fr: StatusMessageProgressBar | None = None,
        base: int = 0,
        gui_rng: int = 1,
        collect_part: int = 1,
    ) -> None:
        datasets = self._cached_datasets
        self._parawiz_sync_cross_dataset_filter_buttons()
        rows = self._parawiz_filtered_rows_list()

        col_count = 1 + 2 * len(datasets)
        cc = max(1, col_count)
        cc_rest = max(0, cc - 1)
        self._table.setColumnCount(cc_rest)
        self._table_header.setColumnCount(cc_rest)
        self._table_frozen.setColumnCount(1)
        self._table.horizontalHeader().setStretchLastSection(False)

        n = len(rows)
        self._table.setRowCount(n)
        self._table_frozen.setRowCount(n)
        self._parawiz_install_frozen_header()
        self._parawiz_install_scroll_headers(datasets)
        self._parawiz_unify_param_header_heights()
        pump_fill = max(1, n // 50)
        for row_idx, (name, by_ds) in enumerate(rows):
            tr = row_idx
            st = self._cached_row_styles.get(name, _PARAWIZ_ROW_STYLE_NEUTRAL)
            disp_name = f"{name} *" if st.star_suffix else name
            row_name = QTableWidgetItem(disp_name)
            _fn_bold: QFont | None = None
            if st.row_bold:
                _fn_bold = QFont(self._table.font())
                _fn_bold.setBold(True)
            fallback_pid: str | None = None
            for ds_name, ds_id in datasets:
                _ = ds_name
                hit = by_ds.get(ds_id)
                if hit is not None:
                    fallback_pid = str(hit[2])
                    break
            if fallback_pid is not None:
                row_name.setData(Qt.ItemDataRole.UserRole, fallback_pid)
            if _fn_bold is not None:
                row_name.setFont(_fn_bold)
            if st.frozen_fg is not None:
                row_name.setForeground(QBrush(st.frozen_fg))
            self._table_frozen.setItem(tr, 0, row_name)

            for i, (_ds_name, ds_id) in enumerate(datasets):
                c_type = 2 * i
                c_val = c_type + 1
                hit = by_ds.get(ds_id)
                if hit is None:
                    _miss_br = MainWindow._parawiz_missing_dataset_brush()
                    it_t = QTableWidgetItem("")
                    it_t.setBackground(_miss_br)
                    it_t.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    it_v = QTableWidgetItem("")
                    it_v.setBackground(_miss_br)
                    it_v.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    self._table.setItem(tr, c_type, it_t)
                    self._table.setItem(tr, c_val, it_v)
                    continue
                ptype, value_repr, pid = hit
                it1 = QTableWidgetItem(ptype)
                it2 = QTableWidgetItem(value_repr)
                pid_s = str(pid)
                it1.setData(Qt.ItemDataRole.UserRole, pid_s)
                it2.setData(Qt.ItemDataRole.UserRole, pid_s)
                if _fn_bold is not None:
                    it1.setFont(_fn_bold)
                    it2.setFont(_fn_bold)
                _cell_fg = st.dataset_col_fg.get(i)
                if _cell_fg is not None:
                    _fgb = QBrush(_cell_fg)
                    it1.setForeground(_fgb)
                    it2.setForeground(_fgb)
                self._table.setItem(tr, c_type, it1)
                self._table.setItem(tr, c_val, it2)

            if dcm_table_progress and fr is not None and self._dcm_import_status_frame_in_bar and n > 0:
                if row_idx % pump_fill == 0 or row_idx == n - 1:
                    fill_prog = collect_part + int(
                        (gui_rng - collect_part) * (row_idx + 1) / float(n)
                    )
                    capped = min(fill_prog, gui_rng - 1)
                    fr.set_value(base + capped)
                    app = QApplication.instance()
                    if app is not None:
                        app.processEvents()

        self._parawiz_update_filter_count_label()
        self._parawiz_uniform_column_widths()
        self._parawiz_reset_param_table_hscroll()
        QTimer.singleShot(0, self._parawiz_fit_window_for_param_table_horizontal)
        QTimer.singleShot(50, self._parawiz_reapply_param_table_host_width_after_layout)

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
            datasets, rows, row_styles = self._collect_rows(
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

        self._cached_datasets = datasets
        self._cached_rows = rows
        self._cached_row_styles = row_styles
        self._parawiz_compare_first_pid = None
        self._populate_table_rows(
            dcm_table_progress=dcm_table_progress,
            fr=fr,
            base=base,
            gui_rng=gui_rng,
            collect_part=collect_part,
        )
        self._parawiz_update_param_table_area_visibility()

        if dcm_table_progress and fr is not None and self._dcm_import_status_frame_in_bar:
            fr.set_value(base + gui_rng)
            fr.set_message("DCM · Tabelle fertig.")

        if not dcm_table_progress:
            flt = self._filter_name.text().strip()
            shown = self._table_frozen.rowCount()
            total = len(self._cached_rows)
            ds_cnt = len(self._cached_datasets)
            if flt:
                self.statusBar().showMessage(
                    f"{shown} / {total} parameters shown across {ds_cnt} dataset(s)",
                    4000,
                )
            else:
                self.statusBar().showMessage(
                    f"{total} parameters loaded across {ds_cnt} dataset(s)",
                    3000,
                )

    def _on_parameter_scroll_table_double_clicked(self, row: int, col: int) -> None:
        self._on_parameter_table_double_clicked_impl(row, col, from_frozen=False)

    def _on_parameter_frozen_table_double_clicked(self, row: int, col: int) -> None:
        _ = col
        self._on_parameter_table_double_clicked_impl(row, 0, from_frozen=True)

    def _parawiz_pid_from_clicked_cell(self, row: int, col: int, *, from_frozen: bool) -> UUID | None:
        nrows = self._table.rowCount()
        tw = self._table_frozen if from_frozen else self._table
        ncol = 1 if from_frozen else self._table.columnCount()
        if row < 0 or row >= nrows:
            return None
        if not from_frozen and (col < 0 or col >= ncol):
            return None
        it = tw.item(row, 0 if from_frozen else col)
        if it is None and not from_frozen:
            it = tw.item(row, 0)
        if it is None and not from_frozen:
            for c in range(ncol):
                it2 = tw.item(row, c)
                if it2 is not None:
                    it = it2
                    break
        if it is None:
            return None
        pid_raw = it.data(Qt.ItemDataRole.UserRole)
        if not pid_raw:
            return None
        try:
            return UUID(str(pid_raw))
        except ValueError:
            return None

    def _on_parameter_scroll_table_clicked(self, row: int, col: int) -> None:
        mods = QApplication.keyboardModifiers()
        if not (mods & Qt.KeyboardModifier.ControlModifier):
            self._parawiz_compare_first_pid = None
            return
        pid = self._parawiz_pid_from_clicked_cell(row, col, from_frozen=False)
        if pid is None:
            return
        if self._parawiz_compare_first_pid is None:
            self._parawiz_compare_first_pid = pid
            self.statusBar().showMessage(
                "Vergleichsmodus: Erste Kennfeld-Zelle gewählt. Mit gedrückter Strg-Taste zweite Zelle anklicken.",
                5000,
            )
            return
        first_pid = self._parawiz_compare_first_pid
        self._parawiz_compare_first_pid = None
        if first_pid == pid:
            self.statusBar().showMessage("Vergleichsmodus: Bitte eine zweite, andere Zelle wählen.", 3500)
            return
        self._open_calibration_map_compare_dialog(first_pid, pid)

    def _open_calibration_map_compare_dialog(self, pid_a: UUID, pid_b: UUID) -> None:
        from synariustools.tools.calmapwidget import (
            CalibrationMapData,
            create_calibration_map_compare_viewer,
            supports_calibration_plot,
        )

        repo = self._controller.model.parameter_runtime().repo
        try:
            rec_a = repo.get_record(pid_a)
            rec_b = repo.get_record(pid_b)
        except Exception:
            return
        if rec_a.name != rec_b.name:
            QMessageBox.information(
                self,
                "ParaWiz",
                "Vergleich nur für denselben Parameternamen möglich. Bitte zwei Zellen desselben Parameters wählen.",
            )
            return
        if not supports_calibration_plot(rec_a) or not supports_calibration_plot(rec_b):
            QMessageBox.information(
                self,
                "ParaWiz",
                "Vergleichsmodus ist nur für numerische Kennlinien/Kennfelder verfügbar.",
            )
            return
        va = np.asarray(rec_a.values, dtype=np.float64)
        vb = np.asarray(rec_b.values, dtype=np.float64)
        if va.ndim != 2 or vb.ndim != 2:
            QMessageBox.information(
                self,
                "ParaWiz",
                "Vergleichsmodus ist aktuell auf Kennfelder (2D) beschränkt.",
            )
            return
        d_a = CalibrationMapData.from_parameter_record(rec_a)
        d_b = CalibrationMapData.from_parameter_record(rec_b)
        ds_a = repo.get_dataset_init_file_stem(rec_a.data_set_id)
        ds_b = repo.get_dataset_init_file_stem(rec_b.data_set_id)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"ParaWiz — Vergleich {rec_a.name}")
        dlg.setWindowIcon(self._app_icon)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        try:
            shell = create_calibration_map_compare_viewer(
                d_a,
                d_b,
                parent=dlg,
                embedded=True,
                left_title=ds_a,
                right_title=ds_b,
            )
        except Exception as exc:
            QMessageBox.critical(self, "ParaWiz", f"Vergleichsfenster konnte nicht erstellt werden:\n{exc}")
            return
        lay.addWidget(shell, 1)
        sh = shell.sizeHint()
        dlg.resize(max(900, sh.width()), max(700, sh.height()))
        self._register_modeless_param_viewer(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_parameter_table_double_clicked_impl(self, row: int, col: int, *, from_frozen: bool) -> None:
        # Mit SelectRows liefert Qt die Doppelklick-Spalte u. U. als 0 statt der angeklickten Spalte —
        # daher jede Spalte der Zeile zulassen.
        pid = self._parawiz_pid_from_clicked_cell(row, col, from_frozen=from_frozen)
        if pid is None:
            return
        try:
            rec = self._controller.model.parameter_runtime().repo.get_record(pid)
        except Exception:
            return
        from synariustools.tools.calmapwidget import (
            CalibrationMapData,
            create_calibration_map_viewer,
            exec_scalar_calibration_edit_dialog,
            supports_calibration_plot,
            supports_calibration_scalar_edit,
        )

        def _on_calmap_applied(viewer: object) -> None:
            self._parawiz_push_calibration_to_model(viewer, pid)

        if not supports_calibration_plot(rec) and not supports_calibration_scalar_edit(rec):
            QMessageBox.information(
                self,
                "ParaWiz",
                "Nur numerische Kenngrößen können hier bearbeitet oder geplottet werden "
                "(Skalar: Wert-Dialog; Kennlinie/Kennfeld/Vektor: Plot).",
            )
            return
        data = CalibrationMapData.from_parameter_record(rec)
        if supports_calibration_scalar_edit(rec):
            new_v = exec_scalar_calibration_edit_dialog(self, data, window_icon=self._app_icon)
            if new_v is not None:
                v_arr = np.asarray(new_v, dtype=np.float64).reshape(())
                self._parawiz_write_calibration_numeric_to_model(pid, data.title, v_arr, {})
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"ParaWiz — {rec.name}")
        dlg.setWindowIcon(self._app_icon)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        shell = create_calibration_map_viewer(
            data, parent=dlg, embedded=True, on_applied_to_model=_on_calmap_applied
        )
        shell.attach_dialog_close_guard(dlg)
        lay.addWidget(shell, 1)
        sh = shell.sizeHint()
        dlg.resize(sh.width(), sh.height())
        self._register_modeless_param_viewer(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
