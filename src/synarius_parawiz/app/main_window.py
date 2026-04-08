"""Main window for Synarius ParaWiz."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np
from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
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
    QMenu,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from synarius_core.controller import CommandError, MinimalController
from synarius_core.model.data_model import ComplexInstance
from synarius_core.parameters.repository import (
    ParameterCompareFingerprints,
    ParameterRecord,
)

import synarius_parawiz as _synarius_parawiz_pkg
from synarius_dataviewer.app import theme
from synarius_parawiz._version import __version__
from synarius_parawiz.app.compat_table_view import CompatTableView
from synarius_parawiz.app.console_window import ConsoleWindow
from synarius_parawiz.app.icon_utils import parawiz_app_icon
from synarius_parawiz.app.parameter_compare_logic import (
    RowCompareSnapshot,
    compute_row_compare_snapshot,
    neutral_row_compare_snapshot,
)
from synarius_parawiz.app.parameter_table_split_view import ParameterTableSplitView
from synarius_parawiz.app.status_progress_widget import StatusMessageProgressBar
from synariustools.tools.plotwidget.svg_icons import icon_from_svg_file, icon_from_tinted_svg_file

_LOG_PERF = logging.getLogger("synarius_parawiz.performance")


def _parawiz_profile_enabled() -> bool:
    v = os.environ.get("SYNARIUS_PARAWIZ_PROFILE", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _parawiz_profile_log(msg: str) -> None:
    """Schreibt Profiling-Zeilen in Logger und stderr (ohne extra Log-Level-Konfiguration)."""
    _LOG_PERF.info(msg)
    print(msg, file=sys.stderr, flush=True)


def _parawiz_effective_cross_style_row_cap(class_default: int) -> int:
    raw = os.environ.get("SYNARIUS_PARAWIZ_CROSS_STYLE_MAX_ROWS", "").strip()
    if not raw:
        return class_default
    try:
        n = int(raw, 10)
        return max(500, min(n, 500_000))
    except ValueError:
        return class_default


@dataclass(slots=True)
class _ParawizRowCrossDsStyle:
    """Vergleich eines Parameters über mehrere Parametersätze (Zeilenformatierung)."""

    row_bold: bool
    star_suffix: bool
    dataset_col_fg: dict[int, QColor]
    frozen_fg: QColor | None


_PARAWIZ_ROW_STYLE_NEUTRAL = _ParawizRowCrossDsStyle(False, False, {}, None)

# cp @selection skipped_details.reason → Kurztext (GUI)
_PARAWIZ_CP_SKIP_REASON_DE: dict[str, str] = {
    "missing_source_id": "Keine Quell-UUID",
    "source_already_in_target_dataset": (
        "Gehört bereits zum aktiven Ziel-Datensatz — es wurde nichts kopiert; "
        "die Target-Spalte zeigt denselben Wert wie die Zielspalte."
    ),
    "no_target_parameter_with_same_name": "Im Ziel fehlt eine Kenngröße mit gleichem Namen",
    "target_parameter_has_no_id": "Ziel-Kenngröße ohne UUID",
}
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
# Fester Modellname: separater Schreib-/Zielsatensatz (nur Target-Spalte), nicht als Vergleichsspalte.
PARAWIZ_TARGET_DATASET_NAME = "parawiz_target"


def _parawiz_build_ccp_select_lines(refs: list[str], *, max_cmd_chars: int) -> list[str]:
    """Split ``refs`` into ``select`` / ``select -p`` CCP lines so each stays under ``max_cmd_chars``."""
    if not refs:
        return []
    chunks: list[list[str]] = []
    cur: list[str] = []
    for r in refs:
        trial = cur + [r]
        first_chunk = len(chunks) == 0
        prefix = "select " if first_chunk else "select -p "
        line_len = len(prefix + " ".join(shlex.quote(x) for x in trial))
        if line_len <= max_cmd_chars:
            cur = trial
            continue
        if cur:
            chunks.append(cur)
            cur = []
        first_chunk = len(chunks) == 0
        prefix = "select " if first_chunk else "select -p "
        lone = prefix + shlex.quote(r)
        if len(lone) > max_cmd_chars:
            chunks.append([r])
            cur = []
            continue
        cur = [r]
    if cur:
        chunks.append(cur)
    out: list[str] = []
    for i, ch in enumerate(chunks):
        if i == 0:
            out.append("select " + " ".join(shlex.quote(x) for x in ch))
        else:
            out.append("select -p " + " ".join(shlex.quote(x) for x in ch))
    return out


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


class _ParawizModelSelectionDelegate(QStyledItemDelegate):
    """Lila Overlay für CCP-``select`` (Modell-Selektion), unabhängig von der Qt-Auswahl (blau, Zwischenablage)."""

    def __init__(self, main_window: "MainWindow", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._main = main_window

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        super().paint(painter, option, index)
        pid = index.data(Qt.ItemDataRole.UserRole)
        if not pid:
            return
        try:
            puuid = UUID(str(pid))
        except ValueError:
            return
        if not self._main._parawiz_parameter_is_in_model_selection_by_pid(puuid):
            return
        c = QColor(theme.PARAWIZ_PARAMETER_SELECTION_BACKGROUND)
        c.setAlpha(120)
        painter.save()
        painter.fillRect(option.rect, c)
        painter.restore()


class MainWindow(QMainWindow):
    # Ab dieser Zeilenzahl keine Cross-Dataset-Vergleichsformatierung (kein get_compare_fingerprints; sonst sehr teuer).
    # Überschreibbar: SYNARIUS_PARAWIZ_CROSS_STYLE_MAX_ROWS (Zahl). Bei sehr großen Listen ggf. Namensfilter nutzen.
    _PARAWIZ_CROSS_STYLE_MAX_ROWS = 12_000
    # CCP-Zeilen für Kennfeld/Kennlinie: in-process ohne Shell-Limits, dennoch abgesichert
    _PARAWIZ_CCP_CMD_MAX_TOTAL = 280_000
    _parawiz_missing_ds_brush_cached: QBrush | None = None
    _parawiz_category_icon_cache: dict[str, QIcon] = {}
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

    # Persistente Kennfarbe der Parametersatz-Spalte (Modellknoten MODEL.PARAMETER_DATA_SET).
    _PARAWIZ_DATASET_HEADER_COLOR_ATTR = "parawiz_header_color"

    @staticmethod
    def _parawiz_filter_clear_icon_black(*, logical_side: int = 16) -> QIcon:
        """Schwarzes Kreuz für den Filter-Clear-Button (heller Hintergrund)."""
        dpr = 1.0
        app = QApplication.instance()
        if app is not None:
            scr = app.primaryScreen()
            if scr is not None:
                dpr = max(1.0, float(scr.devicePixelRatio()))
        px = max(1, int(round(logical_side * dpr)))
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(0, 0, 0))
        pen.setWidthF(max(1.5, 2.0 * dpr))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        m = 0.22 * float(px)
        p.drawLine(int(m), int(m), int(px - m), int(px - m))
        p.drawLine(int(px - m), int(m), int(m), int(px - m))
        p.end()
        pm.setDevicePixelRatio(dpr)
        return QIcon(pm)

    @staticmethod
    def _parawiz_dataset_delete_icon_white(*, logical_side: int = 18) -> QIcon:
        """Weißes Andreaskreuz für „Parametersatz löschen“ auf dem blauen Kopf-Button."""
        dpr = 1.0
        app = QApplication.instance()
        if app is not None:
            scr = app.primaryScreen()
            if scr is not None:
                dpr = max(1.0, float(scr.devicePixelRatio()))
        px = max(1, int(round(logical_side * dpr)))
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(255, 255, 255))
        pen.setWidthF(max(1.5, 2.0 * dpr))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        m = 0.22 * float(px)
        p.drawLine(int(m), int(m), int(px - m), int(px - m))
        p.drawLine(int(px - m), int(m), int(m), int(px - m))
        p.end()
        pm.setDevicePixelRatio(dpr)
        return QIcon(pm)

    @staticmethod
    def _parawiz_cross_dataset_filter_button_stylesheet() -> str:
        """Kompakter Toggle-Button für Gleich/Abweichend mit sichtbarem Icon."""
        return (
            "QToolButton {"
            " background-color: #586cd4;"
            " color: #ffffff;"
            " border: 1px solid #3f51b8;"
            " border-radius: 4px;"
            " padding: 2px;"
            "}"
            "QToolButton:hover { background-color: #6a7ce0; }"
            "QToolButton:pressed { background-color: #4f61c8; }"
            "QToolButton:checked { border: 2px solid #ffffff; }"
            "QToolButton:disabled {"
            " background-color: #6d7482;"
            " border: 1px solid #565c69;"
            "}"
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
        self._parawiz_protocol_backlog: list[tuple[str, str]] = []
        # Modeless plot dialogs (avoid GC + track for optional future use)
        self._open_param_viewers: list[QDialog] = []
        self._dcm_import_status_frame: StatusMessageProgressBar | None = None
        self._dcm_import_status_frame_in_bar = False
        self._dcm_import_file_bytes = 0
        self._dcm_import_write_total = 0
        self._cached_datasets: list[tuple[str, UUID]] = []
        self._cached_rows: list[tuple[str, dict[UUID, tuple[str, str, UUID]]]] = []
        self._cached_row_styles: dict[str, _ParawizRowCrossDsStyle] = {}
        self._cached_row_compare_snapshots: dict[str, RowCompareSnapshot] = {}
        self._parawiz_filtered_rows_cache: list[tuple[str, dict[UUID, tuple[str, str, UUID]]]] | None = None
        self._parawiz_filtered_rows_cache_key: tuple[object, ...] | None = None
        self._parawiz_resize_split_i: int | None = None
        #: ``pair`` = zwei angrenzende Spalten; ``tail`` = nur rechte Kante der letzten Spalte
        self._parawiz_resize_scroll_mode: str | None = None
        self._parawiz_resize_frozen_drag = False
        self._parawiz_resize_start_gx = 0
        self._parawiz_resize_w0 = 0
        self._parawiz_resize_w1 = 0
        self._parawiz_resize_reason: str | None = None
        self._parawiz_copy_in_progress = False
        self._parawiz_compare_first_pid: UUID | None = None
        self._parawiz_compare_first_row: int | None = None
        self._parawiz_compare_first_from_target = False

        self._filter_name = QLineEdit(self)
        self._filter_name.setPlaceholderText("Filter by parameter name… (* ? wildcards)")
        self._filter_name.setClearButtonEnabled(False)
        self._filter_clear_action = QAction(self._filter_name)
        self._filter_clear_action.setIcon(MainWindow._parawiz_filter_clear_icon_black())
        self._filter_clear_action.setToolTip("Filter löschen")
        self._filter_name.addAction(self._filter_clear_action, QLineEdit.ActionPosition.TrailingPosition)
        self._filter_clear_action.triggered.connect(self._on_filter_clear_triggered)
        self._filter_name.textChanged.connect(self._parawiz_update_filter_clear_action_visible)
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
            "QLineEdit QToolButton {"
            " background: transparent;"
            " border: none;"
            " padding: 0px;"
            " margin: 0px 2px 0px 0px;"
            "}"
            "QLineEdit QToolButton:hover {"
            " background-color: rgba(0, 0, 0, 0.08);"
            " border-radius: 2px;"
            "}"
        )

        self._filter_count_label = QLabel("", self)
        self._filter_count_label.setStyleSheet("color: #ffffff; font-size: 9pt; padding: 2px 0px;")
        self._filter_row = QWidget(self)
        _filter_row_lay = QHBoxLayout(self._filter_row)
        _filter_row_lay.setContentsMargins(0, 0, 0, 0)
        _filter_row_lay.setSpacing(10)
        _filter_row_lay.addWidget(self._filter_name, 0)
        _filter_row_lay.addWidget(self._filter_count_label, 0, Qt.AlignmentFlag.AlignVCenter)

        def _mk_filter_toggle_btn(obj_name: str, tip: str, parawiz_icon_svg: str) -> QToolButton:
            b = QToolButton(self._filter_row)
            b.setText("")
            b.setObjectName(obj_name)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setAutoRaise(False)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            b.setStyleSheet(MainWindow._parawiz_cross_dataset_filter_button_stylesheet())
            _ip = MainWindow._parawiz_category_icons_dir() / parawiz_icon_svg
            if _ip.is_file():
                b.setIcon(
                    icon_from_tinted_svg_file(_ip, QColor(theme.STUDIO_TOOLBAR_FOREGROUND), logical_side=18)
                )
            b.setIconSize(QSize(18, 18))
            _fpal = b.palette()
            _fpal.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff"))
            b.setPalette(_fpal)
            b.setFixedSize(28, 28)
            b.setEnabled(False)
            return b

        self._btn_filter_hide_unequal = _mk_filter_toggle_btn(
            "ParaWizFilterHideUnequal",
            "Gleiche: Nur Parameter, die in jedem Parametersatz vorkommen, und nur Zeilen, in denen Werte, Achsen "
            "und Metadaten (ohne UUIDs) in allen Parametersätzen übereinstimmen. Mit dem Namensfilter per "
            "UND verknüpft.",
            "equal.svg",
        )
        self._btn_filter_hide_equal = _mk_filter_toggle_btn(
            "ParaWizFilterHideEqual",
            "Abweichende: Nur Zeilen mit Unterschieden zwischen Parametersätzen (Werte, Achsen oder Metadaten). "
            "Mit dem Namensfilter per UND verknüpft.",
            "nequal.svg",
        )
        self._btn_filter_hide_unequal.toggled.connect(self._on_cross_dataset_filter_toggled)
        self._btn_filter_hide_equal.toggled.connect(self._on_cross_dataset_filter_toggled)
        _filter_row_lay.addWidget(self._btn_filter_hide_unequal, 0, Qt.AlignmentFlag.AlignVCenter)
        _filter_row_lay.addWidget(self._btn_filter_hide_equal, 0, Qt.AlignmentFlag.AlignVCenter)

        _filter_row_lay.addStretch(1)

        self._parawiz_header_scroll_guard = False
        self._parawiz_vscroll_guard = False
        self._parawiz_dataset_title_widgets: set[QObject] = set()

        def _mk_param_table(*, name: str) -> CompatTableView:
            t = CompatTableView(self)
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
        self._table_header_frozen.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table_header_frozen.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._table_frozen = _mk_param_table(name="ParameterTableFrozen")
        self._table_frozen.setAlternatingRowColors(True)
        self._table_frozen.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_frozen.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
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

        # Nur echte cp @selection-Kopien (copied_dst_ids) — Cross-Dataset-Zeilenfärbung „zuletzt aus Zielkopie“.
        self._parawiz_target_db_copied_pids: set[UUID] = set()
        self._parawiz_target_copy_source_col_by_pid: dict[UUID, int] = {}
        self._parawiz_target_overlay_ds_tuple: tuple[UUID, ...] | None = None
        self._parawiz_target_overlay_active: UUID | None = None
        self._parawiz_last_main_focus_col = 0
        self._parawiz_sel_row_guard = False
        self._param_table_split = ParameterTableSplitView(
            self,
            table_factory=lambda name: _mk_param_table(name=name),
            main_column_supplier=lambda: self._parawiz_last_main_focus_col,
        )
        self._table_header = self._param_table_split.main_header
        self._table = self._param_table_split.main_body
        self._table_target_header = self._param_table_split.target_header
        self._table_target = self._param_table_split.target_body
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self._table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self._table_target.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_target.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        for _tw in (self._table, self._table_target):
            _tw.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
            _tw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setAlternatingRowColors(True)
        self._table.itemSelectionChanged.connect(self._parawiz_on_main_table_item_selection_changed)
        # Wie Haupt- und Namensspalte: alternierende Zeilen, damit Zeilenrhythmus und Vergleichsfarben lesbar bleiben.
        self._table_target.setAlternatingRowColors(True)
        for _th in (self._table_header, self._table_target_header):
            _th.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
            _th.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            _th.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            _th.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            _th.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            _th.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._table_target.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._param_table_split.set_target_visible(False)
        # Target-Spalte fest rechts neben der ScrollArea (nicht im QScrollArea-Inhalt), sonst scrollt sie weg.
        self._param_table_target_column = self._param_table_split.take_target_block()
        _corner = QWidget(self._table)
        _corner.setAutoFillBackground(True)
        _cpal = _corner.palette()
        _cpal.setColor(QPalette.ColorRole.Window, QColor("#2f2f2f"))
        _corner.setPalette(_cpal)
        self._table.setCornerWidget(_corner)
        self._param_table_host = self._param_table_split
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
        _table_row_lay.addSpacing(2)
        _table_row_lay.addWidget(self._param_table_target_column, 0)

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
            f"QMenu {{ background-color: {theme.STUDIO_TOOLBAR_COMBO_BACKGROUND}; color: {_wfg}; }}"
            f"QMenu::item:selected {{ background-color: {theme.STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND}; }}"
            f"QStatusBar {{ background-color: {_wbg}; color: {_wfg}; border-top: 1px solid #555555; }}"
            "QStatusBar::item { border: none; padding: 0px; margin: 0px; }"
        )
        _ss_head = self._table_stylesheet_header_tables()
        _ss_body = self._table_stylesheet_body_tables()
        for _tw in (self._table_header_frozen, self._table_header, self._table_target_header):
            _tw.setStyleSheet(_ss_head)
        for _tw in (self._table_frozen, self._table, self._table_target):
            _tw.setStyleSheet(_ss_body)
        self._table.cellDoubleClicked.connect(self._on_parameter_scroll_table_double_clicked)
        self._table_frozen.cellDoubleClicked.connect(self._on_parameter_frozen_table_double_clicked)
        self._table_target.cellDoubleClicked.connect(self._on_parameter_target_table_double_clicked)
        self._table.cellClicked.connect(self._on_parameter_scroll_table_clicked)
        self._table_frozen.cellClicked.connect(self._on_parameter_frozen_table_clicked)
        self._table_target.cellClicked.connect(self._on_parameter_target_table_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_parameter_scroll_context_menu)
        self._table_frozen.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table_frozen.customContextMenuRequested.connect(self._on_parameter_frozen_context_menu)
        self._table_target.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table_target.customContextMenuRequested.connect(self._on_parameter_target_context_menu)
        self._table.horizontalHeader().sectionResized.connect(self._parawiz_on_body_column_resized)
        self._table.horizontalScrollBar().valueChanged.connect(self._parawiz_on_body_hscroll)
        self._table_header.horizontalScrollBar().valueChanged.connect(self._parawiz_on_header_hscroll)
        self._param_table_split.bind_frozen_body(self._table_frozen)
        self._table.setItemDelegate(_ParawizModelSelectionDelegate(self, self._table))
        self._table_target.setItemDelegate(_ParawizModelSelectionDelegate(self, self._table_target))

        for _vp in (
            self._table.viewport(),
            self._table_header.viewport(),
            self._table_target.viewport(),
            self._table_target_header.viewport(),
            self._table_frozen.viewport(),
            self._table_header_frozen.viewport(),
        ):
            _vp.setMouseTracking(True)
            _vp.installEventFilter(self)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self.statusBar().showMessage("Ready")
        self._filter_name.blockSignals(True)
        self._filter_name.setText("*")
        self._filter_name.blockSignals(False)
        self._parawiz_update_filter_clear_action_visible()
        self._parawiz_ensure_target_scratch_dataset()
        self._refresh_table()
        self.statusBar().showMessage(
            "Parameterquelle laden (Register DataSet Source oder Open Parameter Script). "
            "Danach: Doppelklick öffnet Skalar-Editor oder Kennlinie/Kennfeld; "
            "erste Vergleichszelle normal anklicken, zweite mit Strg+Klick (Skalar oder Kennlinie/Kennfeld).",
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

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Hauptfenster zu → gesamte Anwendung beenden (z. B. offenes CLI-Fenster nicht als „letztes Fenster“)."""
        super().closeEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.quit()

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
        try:
            return self._parawiz_event_filter_impl(watched, event)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except BaseException:
            _LOG_PERF.exception(
                "ParaWiz eventFilter: unhandled exception (watched=%r QEvent.Type=%s)",
                watched,
                event.type(),
            )
            return False

    def _parawiz_event_filter_impl(self, watched: QObject, event: QEvent) -> bool:
        tw = self._table
        th = self._table_header
        tfr = self._table_frozen
        tfh = self._table_header_frozen
        min_w = 72
        et = event.type()
        scroll_vps = (tw.viewport(), th.viewport())
        frozen_vps = (tfr.viewport(), tfh.viewport())

        if watched in self._parawiz_dataset_title_widgets and isinstance(event, QMouseEvent):
            if (
                et == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            ):
                ds_raw = watched.property("parawiz_ds_uuid")
                try:
                    ds_id = UUID(str(ds_raw))
                except Exception:
                    return False
                self._parawiz_select_filtered_dataset_parameters(ds_id, append=True)
                return True
            return False

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
        """Graue Kreuzschraffur für fehlende Parameter in einem Satz — auch für die leere Target-Satz-Spalte."""
        if cls._parawiz_missing_ds_brush_cached is None:
            pm = QPixmap(12, 12)
            pm.fill(QColor("#d2d2d2"))
            with QPainter(pm) as p:
                p.setPen(QPen(QColor("#6a6a6a"), 1))
                p.drawLine(0, 12, 12, 0)
                p.drawLine(0, 0, 12, 12)
            cls._parawiz_missing_ds_brush_cached = QBrush(pm)
        return cls._parawiz_missing_ds_brush_cached

    @classmethod
    def _parawiz_category_icons_dir(cls) -> Path:
        # Same root as _parawiz_breeze_icons_dir parent; correct when package is loaded from site-packages.
        return Path(_synarius_parawiz_pkg.__file__).resolve().parent / "icons"

    @classmethod
    def _parawiz_category_icon(cls, category: str) -> QIcon:
        """SVG zur Kategorie in Originalfarben (keine Anpassung an Zelltext)."""
        cat_u = str(category).upper()
        hit = cls._parawiz_category_icon_cache.get(cat_u)
        if hit is not None:
            return hit
        file_names = {
            "VALUE": "value.svg",
            "CURVE": "curve.svg",
            "MAP": "map.svg",
            "MATRIX": "matrix.svg",
            "ARRAY": "array.svg",
            "NODE_ARRAY": "array.svg",
            "ASCII": "value.svg",
        }
        name = file_names.get(cat_u, "value.svg")
        path = cls._parawiz_category_icons_dir() / name
        if not path.is_file():
            path = cls._parawiz_category_icons_dir() / "value.svg"
        icon = icon_from_svg_file(path, logical_side=16)
        cls._parawiz_category_icon_cache[cat_u] = icon
        return icon

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
            (self._act_copy_to_target, "edit-copy.svg"),
            (self._act_clear_selection, "edit-clear-all.svg"),
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
        self._act_copy_to_target.setEnabled(show)
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

    def _parawiz_apply_param_table_total_width(self) -> None:
        min_w = 72
        wf = max(self._table_frozen.columnWidth(0), self._table_header_frozen.columnWidth(0), min_w)
        self._table_frozen.setColumnWidth(0, wf)
        self._table_header_frozen.setColumnWidth(0, wf)
        self._frozen_block.setFixedWidth(wf + 2)

        tw = self._table
        ttw = self._table_target
        tth = self._table_target_header
        n = tw.columnCount()
        nt = ttw.columnCount()
        extra = 6
        if n <= 0 and nt <= 0:
            self._param_table_host.setFixedWidth(0)
            self._param_table_scroll.setVisible(False)
        else:
            self._param_table_scroll.setVisible(True)
            cols_sum = sum(tw.columnWidth(c) for c in range(n)) if n > 0 else 0
            # Vertikale Scrollbar verengt die Viewport-Breite — sonst erscheint ein horizontaler Scrollbalken.
            vs_extra = 0
            if tw.rowCount() > 0:
                vs_extra = max(int(tw.verticalScrollBar().sizeHint().width()), 14)
            total = cols_sum + extra + vs_extra
            th = self._table_header
            # Bei schmalem Fenster: Spalten nicht auf die Viewportbreite quetschen (würde mit der
            # festen Target-Spalte kollidieren). Volle Spaltenbreiten beibehalten — horizontal
            # scrollen; sichtbarer Bereich endet vor der Target-Spalte, der Rest liegt „dahinter“.
            if n > 0:
                # Explizite Breiten: QTableViewport = Widgetbreite − VScroll; bei Policy Fixed war oft zu schmal.
                th.setFixedWidth(int(cols_sum))
                tw.setFixedWidth(int(cols_sum + vs_extra))
            if nt > 0:
                for c in range(nt):
                    w = max(ttw.columnWidth(c), 140)
                    ttw.setColumnWidth(c, w)
                    tth.setColumnWidth(c, w)
                target_cols_sum = sum(ttw.columnWidth(c) for c in range(nt))
                tth.setFixedWidth(int(target_cols_sum))
                ttw.setFixedWidth(int(target_cols_sum))
                self._param_table_split.set_target_fixed_width(int(target_cols_sum))
            # Host (nur Haupttabelle): mindestens Viewportbreite oder Inhalt — Target liegt außerhalb der ScrollArea.
            vp_w = int(self._param_table_scroll.viewport().width())
            # Nur Haupt-Tabellenbreite im Scroll-Inhalt; Target liegt fest rechts neben der ScrollArea.
            self._param_table_host.setFixedWidth(max(int(total), vp_w, 1))
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
            ttw = self._table_target
            tth = self._table_target_header
            n = tw.columnCount()
            if n > 0:
                tw.resizeColumnsToContents()
                for c in range(n):
                    w = max(tw.columnWidth(c), min_w)
                    tw.setColumnWidth(c, w)
                    th.setColumnWidth(c, w)
                datasets = self._cached_datasets
                fm = th.fontMetrics()
                for i in range(len(datasets)):
                    ps_name, _ = datasets[i]
                    if i >= n:
                        break
                    need = fm.horizontalAdvance(str(ps_name)) + 28
                    cur = tw.columnWidth(i)
                    if cur < need:
                        tw.setColumnWidth(i, need)
                        th.setColumnWidth(i, need)
            if ttw.columnCount() > 0:
                ttw.resizeColumnsToContents()
                w_target = max(min_w + 48, ttw.columnWidth(0), tth.columnWidth(0))
                ttw.setColumnWidth(0, w_target)
                tth.setColumnWidth(0, w_target)
            self._parawiz_compact_body_row_heights()
            self._parawiz_apply_param_table_total_width()
        finally:
            self._parawiz_header_scroll_guard = False

    def _parawiz_compact_body_row_heights(self) -> None:
        """Nur Daten-Tabellen: niedrigere Zeilenhöhe (Kopfzeilen-Widgets unverändert)."""
        tw = self._table
        ttw = self._table_target
        tfr = self._table_frozen
        fm = tw.fontMetrics()
        rh = max(16, fm.height() + 1)
        for r in range(tw.rowCount()):
            tw.setRowHeight(r, rh)
        for r in range(ttw.rowCount()):
            ttw.setRowHeight(r, rh)
        for r in range(tfr.rowCount()):
            tfr.setRowHeight(r, rh)

    def _parawiz_fit_window_for_param_table_horizontal(self) -> None:
        """Fenster so weit verbreitern, dass Name + alle Spalten ohne horizontalen Scroll sichtbar sind."""
        cw = self.centralWidget()
        if cw is None:
            return
        target_w = (
            self._param_table_target_column.width() + 2
            if self._param_table_target_column.isVisible()
            else 0
        )
        need = self._frozen_block.width() + self._param_table_host.width() + target_w + 48
        if need <= 0:
            return
        avail = cw.width()
        if need > avail:
            # Synchrones resize() während Layout/Reapply kann zu nicht terminierender Resize-Kaskade führen.
            QTimer.singleShot(0, self._parawiz_fit_window_apply_resize)

    def _parawiz_fit_window_apply_resize(self) -> None:
        """Ein Fenster-Breiten-Tick nach Layout; need/avail erst hier neu messen."""
        cw = self.centralWidget()
        if cw is None:
            return
        target_w = (
            self._param_table_target_column.width() + 2
            if self._param_table_target_column.isVisible()
            else 0
        )
        need = self._frozen_block.width() + self._param_table_host.width() + target_w + 48
        if need <= 0:
            return
        avail = cw.width()
        if need <= avail:
            return
        delta = need - avail
        self._parawiz_resize_reason = "fit_window_apply"
        self.resize(self.width() + delta, self.height())
        self._parawiz_resize_reason = None

    def _parawiz_reapply_param_table_host_width_after_layout(self, *, fit_window: bool = True) -> None:
        """Nach sichtbarer vertikaler Scrollbar Hostbreite erneut setzen (kein horizontaler Scroll im TableWidget)."""
        if self._table.columnCount() <= 0 and self._table_target.columnCount() <= 0:
            return
        self._parawiz_apply_param_table_total_width()
        self._parawiz_sync_param_scroll_geometry()
        if fit_window:
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
        """Alle Wert-Spalten-Zellen für ``pid`` setzen (eine Spalte pro Parametersatz; Skalar-Updates)."""
        tw = self._table
        ncol = tw.columnCount()
        updated = False
        for r in range(tw.rowCount()):
            for c in range(ncol):
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
                    self._parawiz_execute_ccp(cmd, source="parawiz")
                wrote_via_ccp = True
            except CommandError:
                pass

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
                if self._console_window is not None:
                    self._console_window.append_protocol_result(
                        f"# ParaWiz: CCP fehlgeschlagen — gleiche Daten per Repository geschrieben ({title})"
                    )
            else:
                why = "Werte nicht endlich" if not finite else f"CCP-Gesamtlänge {total_len}"
                if self._console_window is not None:
                    self._console_window.append_protocol_result(
                        f"# ParaWiz: Direktschreibung ({why}); shape={tuple(vals.shape)} ref={hr} ({title})"
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

        self._parawiz_refresh_after_model_mutation()
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
        app_inst = QApplication.instance()
        if app_inst is not None:
            app_inst.processEvents()
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
        # CompatTableView is QTableView: duplicate Studio/Dataviewer table chrome for QTableView so
        # alternate row colors (RESOURCES_PANEL_*) apply — QTableWidget-only rules do not match.
        _tbl = (
            f" background-color: {theme.RESOURCES_PANEL_BACKGROUND};"
            f" alternate-background-color: {theme.RESOURCES_PANEL_ALTERNATE_ROW};"
            " color: #1a1a1a;"
            " gridline-color: transparent;"
            " border: none;"
            " font-size: 9pt;"
        )
        _sel = (
            f" background-color: {theme.PARAWIZ_CLIPBOARD_SELECTION_BACKGROUND};"
            f" color: {theme.PARAWIZ_CLIPBOARD_SELECTION_FOREGROUND};"
        )
        return (
            "QTableWidget {"
            + _tbl
            + "}"
            "QTableView {"
            + _tbl
            + "}"
            "QTableWidget::item:selected {"
            + _sel
            + "}"
            "QTableView::item:selected {"
            + _sel
            + "}"
            "QHeaderView::section {"
            " background-color: #353535;"
            " color: #ffffff;"
            " padding: 2px 4px;"
            " border: none;"
            " font-size: 9pt;"
            "}"
            "QScrollBar:vertical { background: #2f2f2f; width: 12px; margin: 0; border: none; }"
            "QScrollBar::handle:vertical { background: #5a5a5a; min-height: 20px; border-radius: 4px; }"
            "QScrollBar::handle:vertical:hover { background: #6a6a6a; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical "
            "{ height: 0; border: none; background: none; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #2f2f2f; }"
            "QScrollBar:horizontal { background: #2f2f2f; height: 12px; margin: 0; border: none; }"
            "QScrollBar::handle:horizontal { background: #5a5a5a; min-width: 20px; border-radius: 4px; }"
            "QScrollBar::handle:horizontal:hover { background: #6a6a6a; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal "
            "{ width: 0; border: none; background: none; }"
            "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: #2f2f2f; }"
            "QAbstractScrollArea::corner { background-color: #2f2f2f; border: none; }"
        )

    def _table_stylesheet_header_tables(self) -> str:
        """Zwei Titelzeilen (Kopf): Item-Padding; keine sichtbaren hellblauen Lücken um Cell-Widgets."""
        return (
            self._table_stylesheet_common()
            + "QTableWidget::item { padding: 0px; border: none; margin: 0px; }"
            + "QTableView::item { padding: 0px; border: none; margin: 0px; }"
            + "QTableWidget#ParameterTableFrozenHeader, QTableWidget#ParameterTableTargetHeader "
            "{ background-color: #525252; alternate-background-color: #525252; }"
            + "QTableView#ParameterTableFrozenHeader, QTableView#ParameterTableTargetHeader "
            "{ background-color: #525252; alternate-background-color: #525252; }"
            + "QTableWidget#ParameterTableFrozenHeader::item, QTableWidget#ParameterTableTargetHeader::item "
            "{ background-color: #525252; color: #ffffff; }"
            + "QTableView#ParameterTableFrozenHeader::item, QTableView#ParameterTableTargetHeader::item "
            "{ background-color: #525252; color: #ffffff; }"
        )

    def _table_stylesheet_body_tables(self) -> str:
        """Parameterliste (Daten) — weniger Abstand oben/unten im Zelltext."""
        return (
            self._table_stylesheet_common()
            + "QTableWidget::item { padding: 0px 2px; margin: 0px; }"
            + "QTableView::item { padding: 0px 2px; margin: 0px; }"
        )

    def _create_actions(self) -> None:
        self._act_open_script = QAction("Open Parameter Script...", self)
        self._act_open_script.triggered.connect(self._open_script)
        self._act_open_source = QAction("Register DataSet Source...", self)
        self._act_open_source.triggered.connect(self._register_data_set_source)
        self._act_refresh = QAction("Refresh", self)
        self._act_refresh.triggered.connect(self._parawiz_refresh_after_model_mutation)
        self._act_clear_selection = QAction("Clear Selection", self)
        self._act_clear_selection.setToolTip("CCP: select (ohne Argumente)")
        self._act_clear_selection.triggered.connect(self._parawiz_clear_model_selection)
        self._act_console = QAction("CLI Console", self)
        self._act_console.triggered.connect(self._open_console)
        self._act_copy_to_target = QAction("Copy Parameters to Target", self)
        self._act_copy_to_target.setToolTip(
            "Kopiert die gewählten Kenngrößen in den separaten Zieldatensatz (rechte Spalte); die geladenen "
            "Vergleichs-DCMs werden nur gelesen. CCP: cp @selection <parawiz_target>. Quelle = fokussierte "
            "bzw. ausgewählte Datensatz-Spalte (lila Modell-Selektion)."
        )
        self._act_copy_to_target.triggered.connect(self._parawiz_copy_selection_to_target_dataset)
        self._act_quit = QAction("Exit ParaWiz", self)
        self._act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self._act_quit.triggered.connect(self.close)
        self._parawiz_set_breeze_action_icons()

    def _create_menus(self) -> None:
        # Classic menu bar hidden; File/View live in toolbar dropdowns (Synarius Studio pattern).
        self.menuBar().setVisible(False)

        file_menu = QMenu("File", self)
        file_menu.addAction(self._act_open_script)
        file_menu.addAction(self._act_open_source)
        file_menu.addSeparator()
        file_menu.addAction(self._act_quit)

        view_menu = QMenu("View", self)
        view_menu.addAction(self._act_refresh)
        view_menu.addAction(self._act_copy_to_target)
        view_menu.addAction(self._act_clear_selection)
        view_menu.addSeparator()
        view_menu.addAction(self._act_console)

        self._file_menu = file_menu
        self._view_menu = view_menu

    def _parawiz_apply_unified_toolbar_chrome(self) -> None:
        tb = getattr(self, "_main_toolbar", None)
        if tb is None:
            return
        tb.setStyleSheet(theme.studio_toolbar_stylesheet())
        for btn in tb.findChildren(QToolButton):
            btn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def _create_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar", self)
        self._main_toolbar = toolbar
        toolbar.setMovable(False)

        file_btn = QToolButton(self)
        file_btn.setText("File")
        file_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        file_btn.setMenu(self._file_menu)
        toolbar.addWidget(file_btn)

        view_btn = QToolButton(self)
        view_btn.setText("View")
        view_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        view_btn.setMenu(self._view_menu)
        toolbar.addWidget(view_btn)

        toolbar.addSeparator()
        toolbar.addAction(self._act_open_script)
        toolbar.addAction(self._act_open_source)
        toolbar.addSeparator()
        toolbar.addAction(self._act_refresh)
        toolbar.addAction(self._act_copy_to_target)
        toolbar.addAction(self._act_clear_selection)
        toolbar.addSeparator()
        toolbar.addAction(self._act_console)

        self.addToolBar(toolbar)
        self._parawiz_apply_unified_toolbar_chrome()

    def _parawiz_flush_protocol_backlog(self) -> None:
        cw = self._console_window
        if cw is None or not self._parawiz_protocol_backlog:
            return
        for kind, text in self._parawiz_protocol_backlog:
            if kind == "cmd":
                cw.append_protocol_command(text)
            elif kind == "out":
                cw.append_protocol_result(text)
            elif kind == "err":
                cw.append_protocol_error(text)
        self._parawiz_protocol_backlog.clear()

    def _open_console(self) -> None:
        if self._console_window is None:
            self._console_window = ConsoleWindow(
                controller=self._controller,
                on_execute_line=self._parawiz_execute_ccp,
                prompt_provider=self._parawiz_console_prompt_path,
                on_command_executed=self._parawiz_on_console_command_executed,
                app_icon=self._app_icon,
            )
        self._parawiz_flush_protocol_backlog()
        self._console_window.show_and_raise()

    def _parawiz_console_prompt_path(self) -> str:
        cur = self._controller.current
        if cur is None:
            return "<none>"
        try:
            return str(cur.get("prompt_path"))
        except Exception:
            return "<none>"

    @staticmethod
    def _parawiz_cli_needs_param_table_refresh(cmd: str) -> bool:
        """REPL-Befehle, die das Parametertabellen-Modell nicht ändern (kein voller Rebuild)."""
        s = cmd.strip()
        if not s:
            return False
        first = s.split(None, 1)[0].lower()
        read_only = frozenset({"ls", "lsattr", "cd", "get"})
        return first not in read_only

    def _parawiz_on_console_command_executed(self, cmd: str) -> None:
        need = self._parawiz_cli_needs_param_table_refresh(cmd)
        if not need:
            return
        self._parawiz_refresh_after_model_mutation()

    def _parawiz_execute_ccp(
        self,
        cmd: str,
        source: str = "parawiz",
        *,
        echo_command: bool | None = None,
        refresh_selection_overlay: bool = True,
    ) -> str | None:
        line = cmd.strip()
        if not line:
            return ""
        cw = self._console_window

        if echo_command is None:
            want_log = source != "repl"
        else:
            want_log = bool(echo_command)
        if want_log:
            if cw is not None:
                cw.append_protocol_command(line)
            elif source != "repl":
                self._parawiz_protocol_backlog.append(("cmd", line))
                if len(self._parawiz_protocol_backlog) > 2000:
                    self._parawiz_protocol_backlog = self._parawiz_protocol_backlog[-2000:]
        try:
            out = self._controller.execute(line)
        except CommandError as exc:
            if cw is not None:
                if source == "repl":
                    cw.append_repl_error(str(exc))
                else:
                    cw.append_protocol_error(str(exc))
            elif want_log and source != "repl":
                self._parawiz_protocol_backlog.append(("err", str(exc)))
                if len(self._parawiz_protocol_backlog) > 2000:
                    self._parawiz_protocol_backlog = self._parawiz_protocol_backlog[-2000:]
            raise
        except Exception as exc:
            if cw is not None:
                if source == "repl":
                    cw.append_repl_error(str(exc))
                else:
                    cw.append_protocol_error(str(exc))
            elif want_log and source != "repl":
                self._parawiz_protocol_backlog.append(("err", str(exc)))
                if len(self._parawiz_protocol_backlog) > 2000:
                    self._parawiz_protocol_backlog = self._parawiz_protocol_backlog[-2000:]
            raise
        # REPL: Ergebnisse immer anzeigen (want_log absichtlich False — keine doppelten Protokoll-Zeilen).
        if source == "repl" and cw is not None and out is not None:
            _text = str(out)
            if _text == "":
                _text = "(no entries)"
            cw.append_repl_result(_text)
        elif want_log and out is not None and str(out) != "":
            if cw is not None:
                cw.append_protocol_result(str(out))
            elif source != "repl":
                self._parawiz_protocol_backlog.append(("out", str(out)))
                if len(self._parawiz_protocol_backlog) > 2000:
                    self._parawiz_protocol_backlog = self._parawiz_protocol_backlog[-2000:]
        if refresh_selection_overlay:
            self._parawiz_refresh_model_selection_overlay()
        return out

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
                self._parawiz_execute_ccp(f'load "{cli_path}"', source="open_script")
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
            self._parawiz_execute_ccp("cd @main/parameters/data_sets", source="register_data_set")
            ds_ref = (
                self._parawiz_execute_ccp(
                    f'new DataSet {name} source_path="{cli_path}" source_format={fmt}',
                    source="register_data_set",
                )
                or ""
            ).strip()
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
        """Nur inhaltlich relevante Metadaten (kein Anzeige-/DCM-Kram wie LANGNAME, EINHEIT, Kommentar).

        Tabellen-Cross-Dataset-Farben nutzen :class:`ParameterCompareFingerprints` (ohne ``source_identifier``).
        """
        si = (str(rec.source_identifier),)
        if rec.is_text:
            return (str(rec.category).upper(),) + si
        return (
            str(rec.category).upper(),
            str(rec.numeric_format),
            str(rec.value_semantics),
        ) + si

    @staticmethod
    def _parawiz_record_full_fingerprint(rec: ParameterRecord) -> tuple:
        return (MainWindow._parawiz_record_va_fingerprint(rec), MainWindow._parawiz_record_meta_fingerprint(rec))

    @staticmethod
    def _parawiz_compare_va_fingerprint(fp: ParameterCompareFingerprints) -> tuple:
        return fp.va_fingerprint

    @staticmethod
    def _parawiz_compare_meta_fingerprint(fp: ParameterCompareFingerprints) -> tuple:
        # Ohne source_identifier: DCM-/Import-Provenance nicht von echten Wert-Unterschieden trennen.
        if fp.is_text:
            return (str(fp.category).upper(),)
        return (
            str(fp.category).upper(),
            str(fp.numeric_format),
            str(fp.value_semantics),
        )

    @staticmethod
    def _parawiz_row_compare_snapshot(
        by_ds: dict[UUID, tuple[str, str, UUID]],
        datasets: list[tuple[str, UUID]],
        *,
        fp_by_id: dict[UUID, ParameterCompareFingerprints],
    ) -> RowCompareSnapshot:
        if len(datasets) < 2:
            return neutral_row_compare_snapshot()
        return compute_row_compare_snapshot(
            by_ds=by_ds,
            datasets=datasets,
            fp_by_id=fp_by_id,
            va_key_fn=MainWindow._parawiz_compare_va_fingerprint,
            meta_key_fn=MainWindow._parawiz_compare_meta_fingerprint,
        )

    @staticmethod
    def _parawiz_style_from_row_compare_snapshot(
        snapshot: RowCompareSnapshot,
        by_ds: dict[UUID, tuple[str, str, UUID]],
        datasets: list[tuple[str, UUID]],
        *,
        active_target_dataset_id: UUID | None = None,
        copied_to_target_pids: frozenset[UUID] | None = None,
    ) -> _ParawizRowCrossDsStyle:
        if len(datasets) < 2 or not snapshot.comparable:
            return _PARAWIZ_ROW_STYLE_NEUTRAL
        # Stern nur für "Werte/Achsen gleich, aber Metadaten verschieden".
        star = snapshot.star_suffix
        palette = _PARAWIZ_DIFF_CLUSTER_HEX
        cpids = copied_to_target_pids or frozenset()
        present_cols = sorted(set(snapshot.value_cluster_by_dataset_col) | set(snapshot.meta_cluster_by_dataset_col))
        if (not snapshot.values_differ) and (not snapshot.meta_differ):
            if active_target_dataset_id is not None and cpids:
                hit_t = by_ds.get(active_target_dataset_id)
                if hit_t is not None and hit_t[2] in cpids:
                    col_neutral: dict[int, QColor] = {}
                    for i in present_cols:
                        col_neutral[i] = QColor(palette[i % len(palette)])
                    return _ParawizRowCrossDsStyle(False, False, col_neutral, None)
            return _PARAWIZ_ROW_STYLE_NEUTRAL
        if snapshot.values_differ:
            # Werte/Achsen unterschiedlich: fett + Clusterfarben in Datensatzspalten,
            # Name-Spalte nur fett (kein Farbcode), damit "gleich/ungleich" klar bleibt.
            row_bold = True
            frozen_fg = None
            cluster_by_col = snapshot.value_cluster_by_dataset_col
        else:
            # Nur Metadaten unterschiedlich: farbig, aber nicht fett.
            row_bold = False
            frozen_fg = QColor(_PARAWIZ_NAME_COL_MIXED_HEX)
            cluster_by_col = snapshot.meta_cluster_by_dataset_col
        col_fg: dict[int, QColor] = {}
        for i, idx in cluster_by_col.items():
            col_fg[i] = QColor(palette[idx % len(palette)])
        return _ParawizRowCrossDsStyle(row_bold, star, col_fg, frozen_fg)

    @staticmethod
    def _parawiz_snapshot_for_row_name(name: str, snapshots: dict[str, RowCompareSnapshot]) -> RowCompareSnapshot:
        return snapshots.get(name, neutral_row_compare_snapshot())

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
        snap = MainWindow._parawiz_snapshot_for_row_name(name, self._cached_row_compare_snapshots)
        n_ds = max(0, len(datasets))
        comparable = snap.comparable
        has_missing_dataset = snap.has_missing_dataset and n_ds >= 2
        has_value_or_axis_diff = bool(snap.row_bold)
        has_metadata_diff_only = bool(snap.star_suffix)
        has_any_difference = has_missing_dataset or has_value_or_axis_diff or has_metadata_diff_only

        if hide_unequal:
            passes_gleiche = True
            if has_missing_dataset:
                passes_gleiche = False
            elif comparable and (has_value_or_axis_diff or has_metadata_diff_only):
                passes_gleiche = False
        else:
            passes_gleiche = True

        if hide_equal:
            passes_abweichende = n_ds >= 2 and has_any_difference
        else:
            passes_abweichende = True

        return passes_gleiche and passes_abweichende

    def _parawiz_filtered_rows_list(
        self,
    ) -> list[tuple[str, dict[UUID, tuple[str, str, UUID]]]]:
        key = (
            self._filter_name.text(),
            self._btn_filter_hide_unequal.isChecked(),
            self._btn_filter_hide_equal.isChecked(),
            id(self._cached_rows),
            id(self._cached_datasets),
            id(self._cached_row_compare_snapshots),
        )
        if self._parawiz_filtered_rows_cache_key == key and self._parawiz_filtered_rows_cache is not None:
            return self._parawiz_filtered_rows_cache
        rows = list(self._cached_rows)
        flt = self._filter_name.text().strip()
        if flt:
            rows = [row for row in rows if _parameter_name_matches_filter(row[0], flt)]
        ds = self._cached_datasets
        if self._btn_filter_hide_unequal.isChecked() or self._btn_filter_hide_equal.isChecked():
            rows = [row for row in rows if self._parawiz_row_passes_cross_dataset_filters(row[0], row[1], ds)]
        self._parawiz_filtered_rows_cache_key = key
        self._parawiz_filtered_rows_cache = rows
        return rows

    def _on_cross_dataset_filter_toggled(self, _checked: bool) -> None:
        self._populate_table_rows(fit_window=False)

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
        _t_collect0 = time.perf_counter()
        rows_by_name: dict[str, dict[UUID, tuple[str, str, UUID]]] = {}
        model = self._controller.model
        repo = model.parameter_runtime().repo
        data_sets_root = model.parameter_runtime().data_sets_root()
        datasets: list[tuple[str, UUID]] = []
        dataset_nodes: list[ComplexInstance] = []
        for ds_node in data_sets_root.children:
            if not isinstance(ds_node, ComplexInstance):
                continue
            if model.is_in_trash_subtree(ds_node) or ds_node.id is None:
                continue
            try:
                if str(ds_node.get("type")) != "MODEL.PARAMETER_DATA_SET":
                    continue
            except KeyError:
                continue
            datasets.append((repo.get_dataset_init_file_stem(ds_node.id), ds_node.id))
            dataset_nodes.append(ds_node)

        staged: list[tuple[UUID, UUID]] = []
        for ds_node in dataset_nodes:
            ds_id = ds_node.id
            if ds_id is None:
                continue
            stack: list[ComplexInstance] = [ds_node]
            while stack:
                cur = stack.pop()
                for child in cur.children:
                    if not isinstance(child, ComplexInstance):
                        continue
                    stack.append(child)
                    try:
                        if str(child.get("type")) != "MODEL.CAL_PARAM":
                            continue
                    except KeyError:
                        continue
                    if child.id is None:
                        continue
                    staged.append((ds_id, child.id))

        summaries = repo.get_parameter_table_summaries_for_ids([pid for _dsid, pid in staged])

        seen = 0
        for ds_id, node_id in staged:
            summary = summaries.get(node_id)
            if summary is None:
                continue
            ds_map = rows_by_name.setdefault(summary.name, {})
            ds_map[ds_id] = (summary.category, summary.value_label, node_id)
            seen += 1
            if seen % seen_every == 0 and on_seen is not None:
                on_seen(seen)
        if on_seen is not None:
            on_seen(seen)
        rows = [(name, rows_by_name[name]) for name in sorted(rows_by_name, key=str.lower)]
        _pairs = list(zip(datasets, dataset_nodes, strict=True))
        _pairs.sort(key=lambda p: (str(p[0][0]).lower(), str(p[0][1])))
        datasets = [p[0] for p in _pairs]
        dataset_nodes = [p[1] for p in _pairs]
        datasets_for_table: list[tuple[str, UUID]] = [
            (stem, did)
            for (stem, did), node in zip(datasets, dataset_nodes, strict=True)
            if node.name != PARAWIZ_TARGET_DATASET_NAME
        ]
        active_target_dataset_id = self._parawiz_scratch_dataset_id()
        copied_ft = frozenset(self._parawiz_target_db_copied_pids)
        row_styles: dict[str, _ParawizRowCrossDsStyle] = {}
        row_snaps: dict[str, RowCompareSnapshot] = {}
        _fp_ms = 0.0
        max_st = _parawiz_effective_cross_style_row_cap(MainWindow._PARAWIZ_CROSS_STYLE_MAX_ROWS)
        if len(datasets_for_table) < 2:
            for name, _bd in rows:
                row_snaps[name] = neutral_row_compare_snapshot()
                row_styles[name] = _PARAWIZ_ROW_STYLE_NEUTRAL
        else:
            _t_fp0 = time.perf_counter()
            need_ids: list[UUID] = []
            for _name, by_ds in rows:
                for _dn, ds_id in datasets_for_table:
                    hit = by_ds.get(ds_id)
                    if hit is not None:
                        need_ids.append(hit[2])
            fp_by_id = repo.get_compare_fingerprints_for_ids(need_ids)
            for name, by_ds in rows:
                row_snaps[name] = MainWindow._parawiz_row_compare_snapshot(
                    by_ds,
                    datasets_for_table,
                    fp_by_id=fp_by_id,
                )
            flt_collect = self._filter_name.text().strip()
            rows_for_style = rows
            if flt_collect and len(rows) > max_st:
                rows_for_style = [row for row in rows if _parameter_name_matches_filter(row[0], flt_collect)]
            names_styled = {r[0] for r in rows_for_style}
            for name, by_ds in rows:
                if name not in names_styled:
                    row_styles[name] = _PARAWIZ_ROW_STYLE_NEUTRAL
                    continue
                row_styles[name] = MainWindow._parawiz_style_from_row_compare_snapshot(
                    MainWindow._parawiz_snapshot_for_row_name(name, row_snaps),
                    by_ds,
                    datasets_for_table,
                    active_target_dataset_id=active_target_dataset_id,
                    copied_to_target_pids=copied_ft,
                )
            _fp_ms = (time.perf_counter() - _t_fp0) * 1000.0
        self._cached_row_compare_snapshots = row_snaps
        if _parawiz_profile_enabled():
            _total_ms = (time.perf_counter() - _t_collect0) * 1000.0
            _parawiz_profile_log(
                "parawiz profile collect: total_ms=%.1f rows=%d fp_ms=%.1f datasets_for_table=%d"
                % (_total_ms, len(rows), _fp_ms, len(datasets_for_table))
            )
        return datasets_for_table, rows, row_styles

    def _apply_filter_to_table(self) -> None:
        self._populate_table_rows(fit_window=False)

    def _on_filter_clear_triggered(self) -> None:
        self._filter_name.clear()

    def _parawiz_update_filter_clear_action_visible(self) -> None:
        self._filter_clear_action.setVisible(bool(self._filter_name.text()))

    @staticmethod
    def _parawiz_header_banner_item(text: str, bg_hex: str) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setTextAlignment(int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
        it.setBackground(QBrush(QColor(bg_hex)))
        it.setForeground(QBrush(QColor("#ffffff")))
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return it

    def _parawiz_install_frozen_header(self) -> None:
        """Linke Kopfspalte: leere erste Kopfzeile / Parameter Name (scrollt nicht horizontal)."""
        t = self._table_header_frozen
        if hasattr(t, "clearSpans"):
            t.clearSpans()
        t.setColumnCount(1)
        t.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
        t.setItem(0, 0, self._parawiz_header_banner_item("", "#525252"))
        t.setItem(1, 0, self._parawiz_header_banner_item("Parameter Name", "#525252"))
        t.resizeRowsToContents()

    def _parawiz_install_scroll_headers(
        self, datasets: list[tuple[str, UUID]], *, show_target_column: bool
    ) -> None:
        """Scrollende Kopfzeilen: Hauptspalten + rechte Zielspalte."""
        t = self._table_header
        tt = self._table_target_header
        self._parawiz_dataset_title_widgets.clear()
        if hasattr(t, "clearSpans"):
            t.clearSpans()
        if hasattr(tt, "clearSpans"):
            tt.clearSpans()
        for ci in range(t.columnCount()):
            for ri in (0, 1):
                cw = t.cellWidget(ri, ci)
                if cw is not None:
                    t.removeCellWidget(ri, ci)
                    cw.deleteLater()
        cc_main = len(datasets)
        t.setColumnCount(cc_main)
        tt.setColumnCount(1 if show_target_column else 0)
        self._param_table_split.set_target_visible(show_target_column)
        if cc_main == 0:
            t.setRowCount(0)
            t.setFixedHeight(0)
            if show_target_column:
                tt.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
                tt.setItem(0, 0, self._parawiz_header_banner_item("Target DataSet", "#525252"))
                tt.setItem(1, 0, self._parawiz_header_banner_item("", "#525252"))
                tt.resizeRowsToContents()
            else:
                tt.setRowCount(0)
                tt.setFixedHeight(0)
            return
        t.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
        tt.setRowCount(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
        ds_nodes = [self._parawiz_parameter_dataset_node(ds_uuid) for _ps_name, ds_uuid in datasets]
        header_colors = [
            self._parawiz_resolve_parawiz_header_color(n, i) for i, n in enumerate(ds_nodes)
        ]
        for i, (ps_name, ds_uuid) in enumerate(datasets):
            color = header_colors[i]
            top_wrap = QWidget(t)
            top_wrap.setProperty("parawiz_ds_uuid", str(ds_uuid))
            top_wrap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            top_wrap.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
            top_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            top_wrap.setStyleSheet(f"background-color: {color}; border: none; margin: 0px; padding: 0px;")
            top_lay = QHBoxLayout(top_wrap)
            top_lay.setContentsMargins(4, 0, 4, 0)
            top_lay.setSpacing(0)
            top_lbl = QLabel(ps_name, top_wrap)
            top_lbl.setProperty("parawiz_ds_uuid", str(ds_uuid))
            top_lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
            top_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            top_lay.addWidget(top_lbl, 1)
            for w in (top_wrap, top_lbl):
                w.installEventFilter(self)
                self._parawiz_dataset_title_widgets.add(w)
                w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                w.customContextMenuRequested.connect(
                    lambda pos, ww=w, du=ds_uuid: self._on_dataset_title_context_menu(ww, pos, du)
                )
            t.setCellWidget(0, i, top_wrap)
        for i, (_ps_name, ds_uuid) in enumerate(datasets):
            color = header_colors[i]
            t.setItem(1, i, self._parawiz_header_banner_item("", color))
            wrap = QWidget(t)
            wrap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            wrap.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
            wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            wrap.setStyleSheet(
                f"background-color: {color}; border: none; margin: 0px; padding: 2px 4px;"
            )
            wrap.setAutoFillBackground(True)
            wpal = wrap.palette()
            wpal.setColor(QPalette.ColorRole.Window, QColor(color))
            wrap.setPalette(wpal)
            hlay = QHBoxLayout(wrap)
            hlay.setContentsMargins(0, 0, 0, 0)
            hlay.setSpacing(0)
            btn = QToolButton(wrap)
            btn.setAutoRaise(False)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            btn.setStyleSheet(
                "QToolButton { background-color: #2563eb; color: #ffffff; border: 1px solid #1d4ed8; "
                "border-radius: 3px; padding: 2px; min-width: 22px; min-height: 22px; }"
                "QToolButton:hover { background-color: #1d4ed8; border: 1px solid #1e40af; }"
                "QToolButton:pressed { background-color: #1e40af; border: 1px solid #1e3a8a; }"
            )
            act = QAction(wrap)
            act.setText("")
            act.setIcon(MainWindow._parawiz_dataset_delete_icon_white(logical_side=18))
            act.setToolTip(
                "Parametersatz inkl. Kenngrößen aus Modell und DuckDB entfernen (entspricht CCP: del <DataSetRef>)."
            )
            act.triggered.connect(lambda _c=False, du=ds_uuid: self._parawiz_on_delete_parameter_dataset(du))
            btn.setDefaultAction(act)
            hlay.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
            hlay.addStretch(1)
            t.setCellWidget(1, i, wrap)
        tt.setItem(0, 0, self._parawiz_header_banner_item("Target DataSet", "#525252"))
        tt.setItem(1, 0, self._parawiz_header_banner_item("", "#525252"))
        t.resizeRowsToContents()
        tt.resizeRowsToContents()
        if t.rowCount() >= 2 and t.columnCount() > 0:
            t.setRowHeight(1, max(t.rowHeight(1), t.rowHeight(0)))

    def _parawiz_parameter_dataset_node(self, data_set_id: UUID) -> ComplexInstance | None:
        root = self._controller.model.parameter_runtime().data_sets_root()
        for c in root.children:
            if not isinstance(c, ComplexInstance):
                continue
            if c.id != data_set_id:
                continue
            try:
                if str(c.get("type")) != "MODEL.PARAMETER_DATA_SET":
                    continue
            except KeyError:
                continue
            return c
        return None

    def _parawiz_collect_used_parawiz_header_colors(self) -> set[str]:
        """Bereits vergebene ParaWiz-Kopfarben (normiert kleingeschrieben) aller Parametersatzknoten."""
        out: set[str] = set()
        root = self._controller.model.parameter_runtime().data_sets_root()
        key = MainWindow._PARAWIZ_DATASET_HEADER_COLOR_ATTR
        for c in root.children:
            if not isinstance(c, ComplexInstance):
                continue
            try:
                if str(c.get("type")) != "MODEL.PARAMETER_DATA_SET":
                    continue
            except KeyError:
                continue
            if key not in c.attribute_dict:
                continue
            try:
                hx = str(c.get(key)).strip().lower()
                if hx.startswith("#") and len(hx) >= 4:
                    out.add(hx)
            except Exception:
                continue
        return out

    def _parawiz_resolve_parawiz_header_color(
        self,
        ds_node: ComplexInstance | None,
        column_index: int,
    ) -> str:
        """Kopffarbe: aus AttributeDict des Dataset-Knotens, sonst freie Palettenfarbe zuorden und speichern."""
        palette = self._DATASET_HEADER_COLORS
        n = len(palette)
        key = MainWindow._PARAWIZ_DATASET_HEADER_COLOR_ATTR
        if ds_node is not None and key in ds_node.attribute_dict:
            try:
                stored = str(ds_node.get(key)).strip()
                low = stored.lower()
                if low.startswith("#") and len(low) >= 4:
                    return low
            except Exception:
                pass
        start = column_index % n
        fallback = palette[start]
        if ds_node is None:
            return fallback
        used = self._parawiz_collect_used_parawiz_header_colors()
        chosen = fallback
        for step in range(n):
            cand = palette[(start + step) % n]
            if cand.lower() not in used:
                chosen = cand
                break
        dict.__setitem__(
            ds_node.attribute_dict,
            key,
            (chosen, None, None, True, True),
        )
        ds_node._touch()
        return chosen

    def _parawiz_dataset_hash_name(self, data_set_id: UUID) -> str | None:
        node = self._parawiz_parameter_dataset_node(data_set_id)
        return node.hash_name if node is not None else None

    def _parawiz_on_delete_parameter_dataset(self, data_set_id: UUID) -> None:
        hn = self._parawiz_dataset_hash_name(data_set_id)
        if not hn:
            QMessageBox.warning(self, "ParaWiz", "Parametersatz nicht im Modell gefunden.")
            return
        repo = self._controller.model.parameter_runtime().repo
        try:
            stem = repo.get_dataset_init_file_stem(data_set_id)
        except Exception:
            stem = str(data_set_id)[:8] + "…"
        cmd = f"del {shlex.quote(hn)}"
        r = QMessageBox.question(
            self,
            "Parametersatz löschen",
            f"Soll der Parametersatz „{stem}“ inklusive aller zugehörigen Kenngrößen aus dem Modell und der "
            f"DuckDB-Datenbank entfernt werden?\n\nEntspricht dem CCP-Befehl:\n{cmd}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        fr_del = self._dcm_import_ensure_status_frame()
        fr_del.set_range(0, 0)
        fr_del.set_message("Löschen · Modell und Datenbank werden aktualisiert …")
        app0 = QApplication.instance()
        if app0 is not None:
            app0.processEvents()
        try:
            self._parawiz_execute_ccp(cmd, source="delete_dataset")
        except CommandError as exc:
            self._dcm_import_remove_progress_bar()
            QMessageBox.critical(self, "ParaWiz", str(exc))
            return
        self._parawiz_refresh_after_model_mutation(delete_progress=True)

    def _parawiz_unify_param_header_heights(self) -> None:
        hf = sum(self._table_header_frozen.rowHeight(r) for r in range(MainWindow.PARAWIZ_TABLE_HEADER_ROWS))
        self._table_header_frozen.setFixedHeight(max(hf, 40))
        if (
            self._table_header.columnCount() > 0
            and self._table_header.rowCount() >= MainWindow.PARAWIZ_TABLE_HEADER_ROWS
        ):
            hs = sum(self._table_header.rowHeight(r) for r in range(MainWindow.PARAWIZ_TABLE_HEADER_ROWS))
            ht = 0
            if self._table_target_header.columnCount() > 0:
                ht = sum(
                    self._table_target_header.rowHeight(r)
                    for r in range(MainWindow.PARAWIZ_TABLE_HEADER_ROWS)
                )
            h = max(hf, hs, ht, 40)
            self._table_header_frozen.setFixedHeight(h)
            self._table_header.setFixedHeight(h)
            if self._table_target_header.columnCount() > 0:
                self._table_target_header.setFixedHeight(h)

    def _populate_table_rows(
        self,
        *,
        dcm_table_progress: bool = False,
        fr: StatusMessageProgressBar | None = None,
        base: int = 0,
        gui_rng: int = 1,
        collect_part: int = 1,
        fit_window: bool = True,
    ) -> None:
        _t_pop0 = time.perf_counter()
        datasets = self._cached_datasets
        self._parawiz_sync_cross_dataset_filter_buttons()
        rows = self._parawiz_filtered_rows_list()

        scratch_ds_id = self._parawiz_scratch_dataset_id()

        cc_main = len(datasets)
        target_enabled = len(datasets) > 0 or scratch_ds_id is not None
        self._table.setColumnCount(cc_main)
        self._table_header.setColumnCount(cc_main)
        self._table_target.setColumnCount(1 if target_enabled else 0)
        self._table_target_header.setColumnCount(1 if target_enabled else 0)
        self._param_table_split.set_target_visible(target_enabled)
        self._table_frozen.setColumnCount(1)
        self._table.horizontalHeader().setStretchLastSection(False)

        n = len(rows)
        self._table.setRowCount(n)
        self._table_target.setRowCount(n)
        self._table_frozen.setRowCount(n)
        self._parawiz_install_frozen_header()
        self._parawiz_install_scroll_headers(datasets, show_target_column=target_enabled)
        self._parawiz_unify_param_header_heights()

        ds_tuple = tuple(ds_id for _n, ds_id in datasets)
        if (
            ds_tuple != self._parawiz_target_overlay_ds_tuple
            or scratch_ds_id != self._parawiz_target_overlay_active
        ):
            self._parawiz_target_db_copied_pids.clear()
            self._parawiz_target_copy_source_col_by_pid.clear()
        self._parawiz_target_overlay_ds_tuple = ds_tuple
        self._parawiz_target_overlay_active = scratch_ds_id

        pump_fill = max(1, n // 50)
        for row_idx, (name, by_ds) in enumerate(rows):
            tr = row_idx
            empty_dataset_brush = MainWindow._parawiz_missing_dataset_brush()
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
                hit = by_ds.get(ds_id)
                if hit is None:
                    it_m = QTableWidgetItem("")
                    it_m.setBackground(empty_dataset_brush)
                    it_m.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    self._table.setItem(tr, i, it_m)
                    continue
                ptype, value_repr, pid = hit
                _cell_fg = st.dataset_col_fg.get(i)
                icon = MainWindow._parawiz_category_icon(ptype)
                it_m = QTableWidgetItem(value_repr)
                it_m.setIcon(icon)
                pid_s = str(pid)
                it_m.setData(Qt.ItemDataRole.UserRole, pid_s)
                if _fn_bold is not None:
                    it_m.setFont(_fn_bold)
                if _cell_fg is not None:
                    it_m.setForeground(QBrush(_cell_fg))
                self._table.setItem(tr, i, it_m)
            if target_enabled:
                hit_t = by_ds.get(scratch_ds_id) if scratch_ds_id is not None else None
                # Sichtbarkeit aus Repo (GUI-, CLI- und sonstige cp @selection); kein separates Overlay-Set.
                show_tgt = hit_t is not None
                if not show_tgt:
                    it_target = QTableWidgetItem("")
                    it_target.setBackground(empty_dataset_brush)
                    it_target.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    self._table_target.setItem(tr, 0, it_target)
                else:
                    ptype_t, value_repr_t, pid_t = hit_t
                    icon_t = MainWindow._parawiz_category_icon(ptype_t)
                    it_target = QTableWidgetItem(value_repr_t)
                    it_target.setIcon(icon_t)
                    it_target.setData(Qt.ItemDataRole.UserRole, str(pid_t))
                    src_col_for_target = self._parawiz_target_copy_source_col_by_pid.get(pid_t)
                    fn_t = QFont(self._table.font())
                    fn_t.setBold(src_col_for_target is not None)
                    it_target.setFont(fn_t)
                    _tfg = None
                    if src_col_for_target is not None:
                        # Quellspalte stabil kodieren (nicht clusterabhängig), damit Copy-Herkunft sichtbar bleibt.
                        _tfg = QColor(
                            _PARAWIZ_DIFF_CLUSTER_HEX[src_col_for_target % len(_PARAWIZ_DIFF_CLUSTER_HEX)]
                        )
                    # region agent log
                    try:
                        import time as _agent_time

                        _dbg_path = Path(__file__).resolve().parents[4] / "debug-788002.log"
                        with _dbg_path.open("a", encoding="utf-8") as _df:
                            _df.write(
                                json.dumps(
                                    {
                                        "sessionId": "788002",
                                        "runId": "post-fix-color",
                                        "hypothesisId": "H10",
                                        "timestamp": int(_agent_time.time() * 1000),
                                        "location": "main_window._populate_table_rows:target_color",
                                        "message": "target_color_from_source_col",
                                        "data": {
                                            "row": tr,
                                            "pid_t": str(pid_t),
                                            "src_col_for_target": src_col_for_target,
                                            "source_color_hex": (
                                                _PARAWIZ_DIFF_CLUSTER_HEX[
                                                    src_col_for_target % len(_PARAWIZ_DIFF_CLUSTER_HEX)
                                                ]
                                                if src_col_for_target is not None
                                                else None
                                            ),
                                            "has_color": _tfg is not None,
                                        },
                                    },
                                    default=str,
                                )
                                + "\n"
                            )
                    except Exception:
                        pass
                    # endregion
                    if _tfg is not None:
                        it_target.setForeground(QBrush(_tfg))
                    self._table_target.setItem(tr, 0, it_target)
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

        self._parawiz_refresh_model_selection_overlay()
        self._parawiz_update_filter_count_label()
        self._parawiz_uniform_column_widths()
        self._parawiz_reset_param_table_hscroll()
        if _parawiz_profile_enabled():
            _pop_ms = (time.perf_counter() - _t_pop0) * 1000.0
            _parawiz_profile_log(
                "parawiz profile populate: total_ms=%.1f rows=%d cols=%d target=%s"
                % (_pop_ms, n, cc_main, target_enabled)
            )
        if fit_window:
            QTimer.singleShot(0, self._parawiz_fit_window_for_param_table_horizontal)
        QTimer.singleShot(
            50,
            lambda fw=fit_window: self._parawiz_reapply_param_table_host_width_after_layout(fit_window=fw),
        )

    def _refresh_table(
        self,
        *,
        dcm_table_progress: bool = False,
        dcm_imported_hint: int = 1,
        delete_table_progress: bool = False,
        fit_window: bool = True,
    ) -> None:
        _t_refresh0 = time.perf_counter()
        fr = self._dcm_import_status_frame
        t = self._dcm_import_write_total
        if delete_table_progress:
            fr = self._dcm_import_ensure_status_frame()
            fr.set_range(0, 0)
            fr.set_message("Parameterliste wird neu geladen …")
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
            base = 0
            gui_rng = 1
            collect_part = 1
        else:
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

        def _on_delete_collect_seen(seen: int) -> None:
            if fr is None or not self._dcm_import_status_frame_in_bar:
                return
            fr.set_message(f"Löschen · Parameterliste wird eingelesen ({seen}) …")
            app = QApplication.instance()
            if app is not None:
                app.processEvents()

        collect_cb: Callable[[int], None] | None = None
        if dcm_table_progress:
            collect_cb = _on_collect_seen
        elif delete_table_progress:
            collect_cb = _on_delete_collect_seen

        self._parawiz_ensure_target_scratch_dataset()
        try:
            datasets, rows, row_styles = self._collect_rows(on_seen=collect_cb)
        except ModuleNotFoundError as exc:
            if delete_table_progress:
                self._dcm_import_remove_progress_bar()
            QMessageBox.critical(
                self,
                "Missing dependency",
                f"{exc}\n\nInstall dependencies for synarius-apps, then restart ParaWiz.",
            )
            return
        except Exception:
            raise

        n = len(rows)
        if delete_table_progress and fr is not None:
            gui_rng = self._dcm_table_bar_slots(max(n, 1))
            collect_part = max(1, gui_rng // 2)
            base = 0
            fr.set_range(0, gui_rng)
            fr.set_value(0)
            fr.set_message(f"Löschen · Tabelle wird aufgebaut ({n} Zeilen) …")
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        elif dcm_table_progress and fr is not None and self._dcm_import_status_frame_in_bar:
            fr.set_value(base + collect_part)
            fr.set_message("DCM · Tabelle · Zellen einfügen …")

        self._cached_datasets = datasets
        self._cached_rows = rows
        self._cached_row_styles = row_styles
        self._parawiz_compare_first_pid = None
        self._parawiz_compare_first_row = None
        table_progress = dcm_table_progress or delete_table_progress
        self._populate_table_rows(
            dcm_table_progress=table_progress,
            fr=fr,
            base=base,
            gui_rng=gui_rng,
            collect_part=collect_part,
            fit_window=fit_window,
        )
        self._parawiz_update_param_table_area_visibility()

        if dcm_table_progress and fr is not None and self._dcm_import_status_frame_in_bar:
            fr.set_value(base + gui_rng)
            fr.set_message("DCM · Tabelle fertig.")

        if delete_table_progress and fr is not None and self._dcm_import_status_frame_in_bar:
            fr.set_value(base + gui_rng)
            fr.set_message("Tabelle aktualisiert.")
            self._dcm_import_remove_progress_bar()
            self.statusBar().showMessage("Parametersatz gelöscht.", 6000)
        elif not dcm_table_progress and not delete_table_progress:
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
        if _parawiz_profile_enabled():
            _parawiz_profile_log(
                "parawiz profile refresh: total_ms=%.1f rows=%d"
                % ((time.perf_counter() - _t_refresh0) * 1000.0, len(self._cached_rows))
            )

    def _parawiz_refresh_after_model_mutation(
        self,
        *,
        delete_progress: bool = False,
        fit_window: bool = True,
        on_layout_complete: Callable[[], None] | None = None,
    ) -> None:
        """Tabelle aus Modell neu; Hostbreite/Scroll nach CCP (z. B. ``del``) synchron.

        Passt die geänderte Spaltenzahl an.
        ``on_layout_complete`` läuft nach erstem Layout/Viewport-Update (``QTimer(0)``), z. B. Selektion löschen.
        """
        self._refresh_table(delete_table_progress=delete_progress, fit_window=fit_window)

        def _post_refresh() -> None:
            self._parawiz_reapply_param_table_host_width_after_layout(fit_window=fit_window)
            for tw in (
                self._table,
                self._table_header,
                self._table_frozen,
                self._table_header_frozen,
                self._table_target,
                self._table_target_header,
            ):
                tw.viewport().update()
            if on_layout_complete is not None:
                on_layout_complete()

        QTimer.singleShot(0, _post_refresh)

    def _on_parameter_scroll_table_double_clicked(self, row: int, col: int) -> None:
        self._on_parameter_table_double_clicked_impl(row, col, from_frozen=False)

    def _on_parameter_frozen_table_double_clicked(self, row: int, col: int) -> None:
        _ = col
        self._on_parameter_table_double_clicked_impl(row, 0, from_frozen=True)

    def _on_parameter_target_table_double_clicked(self, row: int, col: int) -> None:
        _ = col
        self._on_parameter_table_double_clicked_impl(row, 0, from_frozen=False, from_target=True)

    def _on_parameter_target_table_clicked(self, row: int, col: int) -> None:
        _ = col
        self._on_parameter_compare_gesture(row, col=0, from_frozen=False, from_target=True)

    def _parawiz_pid_from_clicked_cell(
        self, row: int, col: int, *, from_frozen: bool = False, from_target: bool = False
    ) -> UUID | None:
        nrows = self._table.rowCount()
        if from_target:
            if row < 0 or row >= nrows or row >= self._table_target.rowCount():
                return None
            it = self._table_target.item(row, 0)
            if it is None:
                return None
            pid_raw = it.data(Qt.ItemDataRole.UserRole)
            if not pid_raw:
                return None
            try:
                return UUID(str(pid_raw))
            except ValueError:
                return None
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

    def _parawiz_hash_name_for_parameter_id(self, pid: UUID) -> str | None:
        obj = self._controller.model.find_by_id(pid)
        if obj is None:
            return None
        return str(getattr(obj, "hash_name", "")) or None

    def _parawiz_selected_pid_str_set(self) -> set[str]:
        """String-IDs der Modell-Selektion (wie CCP ``select``) — für Tabellenzeilen-Zuordnung zuverlässig."""
        out: set[str] = set()
        for obj in self._controller.selection:
            oid = getattr(obj, "id", None)
            if oid is not None:
                out.add(str(oid))
        return out

    def _parawiz_parameter_is_in_model_selection(self, pid: UUID, ref: str | None) -> bool:
        """Ob dieselbe Kenngröße wie bei CCP ``select`` in der Modell-Selektion liegt (id und optional hash_name)."""
        try:
            pid_n = pid if isinstance(pid, UUID) else UUID(str(pid))
        except (ValueError, TypeError, AttributeError):
            return False
        r = (ref or "").strip()
        for obj in self._controller.selection:
            oid = getattr(obj, "id", None)
            if oid is not None:
                try:
                    oid_n = oid if isinstance(oid, UUID) else UUID(str(oid))
                    if oid_n == pid_n:
                        return True
                except (ValueError, TypeError):
                    pass
            if r:
                hn = getattr(obj, "hash_name", None)
                if hn is not None and str(hn) == r:
                    return True
        return False

    def _parawiz_parameter_is_in_model_selection_by_pid(self, pid: UUID) -> bool:
        return self._parawiz_parameter_is_in_model_selection(
            pid, self._parawiz_hash_name_for_parameter_id(pid)
        )

    def _parawiz_refresh_model_selection_overlay(self) -> None:
        """Modell-Selektion (``select``) wird nur per Delegate übermalt, nicht als Qt-Selection."""
        self._table.viewport().update()
        self._table_target.viewport().update()

    def _parawiz_filtered_row_for_parameter(self, pid: UUID) -> int | None:
        rows = self._parawiz_filtered_rows_list()
        datasets = self._cached_datasets
        for row_i, (_name, by_ds) in enumerate(rows):
            for _dn, ds_id in datasets:
                hit = by_ds.get(ds_id)
                if hit is not None and hit[2] == pid:
                    return row_i
        return None

    def _parawiz_selected_pids_in_row(self, row: int) -> list[UUID]:
        rows_list = self._parawiz_filtered_rows_list()
        if row < 0 or row >= len(rows_list):
            return []
        _name, by_ds = rows_list[row]
        datasets = self._cached_datasets
        pid_strs = self._parawiz_selected_pid_str_set()
        out: list[UUID] = []
        seen: set[str] = set()
        for _dn, ds_id in datasets:
            hit = by_ds.get(ds_id)
            if hit is None:
                continue
            opid = hit[2]
            ps = str(opid)
            matched = ps in pid_strs
            if not matched:
                ref = self._parawiz_hash_name_for_parameter_id(opid)
                matched = self._parawiz_parameter_is_in_model_selection(opid, ref)
            if matched and ps not in seen:
                seen.add(ps)
                out.append(opid)
        return out

    def _parawiz_pid_for_frozen_row_model_action(self, row: int) -> UUID | None:
        """Namenspalte (Alt+Klick / Kontext): nicht nur erster Satz.

        Selektion in der Zeile oder zuletzt fokussierte Datenspalte.
        """
        sel_in = self._parawiz_selected_pids_in_row(row)
        if len(sel_in) >= 1:
            return sel_in[0]
        rows_list = self._parawiz_filtered_rows_list()
        if row < 0 or row >= len(rows_list):
            return None
        _name, by_ds = rows_list[row]
        datasets = self._cached_datasets
        fc = self._parawiz_last_main_focus_col
        if 0 <= fc < len(datasets):
            hit = by_ds.get(datasets[fc][1])
            if hit is not None:
                return hit[2]
        for _dn, ds_id in datasets:
            hit = by_ds.get(ds_id)
            if hit is not None:
                return hit[2]
        return None

    def _parawiz_on_main_table_item_selection_changed(self) -> None:
        sm = self._table.selectionModel()
        if sm is None or self._parawiz_sel_row_guard:
            return
        cur = sm.currentIndex()
        if cur.isValid() and cur.column() >= 0:
            self._parawiz_last_main_focus_col = int(cur.column())
        by_row: dict[int, list] = {}
        for ix in sm.selectedIndexes():
            by_row.setdefault(ix.row(), []).append(ix)
        to_deselect = []
        for row, ixs in by_row.items():
            if len(ixs) <= 1:
                continue
            keep = cur if (cur.isValid() and cur.row() == row) else ixs[-1]
            for ix in ixs:
                if ix.row() != keep.row() or ix.column() != keep.column():
                    to_deselect.append(ix)
        if to_deselect:
            self._parawiz_sel_row_guard = True
            try:
                for ix in to_deselect:
                    sm.select(ix, QItemSelectionModel.SelectionFlag.Deselect)
            finally:
                self._parawiz_sel_row_guard = False

    def _parawiz_toggle_model_selection_for_parameter(self, pid: UUID) -> None:
        """Alt+Klick / additiv: bereits selektiert → aus Modell-Selektion entfernen (``select -m``)."""
        ref = self._parawiz_hash_name_for_parameter_id(pid)
        if not ref:
            self.statusBar().showMessage("Parameter ohne hash_name — Modell-Selektion nicht möglich.", 5000)
            return
        if self._parawiz_parameter_is_in_model_selection(pid, ref):
            try:
                self._parawiz_execute_ccp(
                    f"select -m {shlex.quote(ref)}",
                    source="parawiz-select",
                    refresh_selection_overlay=True,
                )
            except CommandError as exc:
                self.statusBar().showMessage(f"Selektion: {exc}", 5000)
                return
            self.statusBar().showMessage("Parameter aus Modell-Selektion entfernt.", 3500)
            return
        n = self._parawiz_select_parameter_ids([pid], append=True)
        if n:
            self.statusBar().showMessage("Parameter zur Modell-Selektion hinzugefügt.", 3500)

    def _parawiz_select_parameter_ids(
        self,
        pids: list[UUID],
        *,
        append: bool,
        enforce_row_uniqueness: bool = True,
    ) -> int:
        if not pids:
            return 0
        if not append:
            refs: list[str] = []
            for pid in pids:
                ref = self._parawiz_hash_name_for_parameter_id(pid)
                if ref:
                    refs.append(ref)
            if not refs:
                return 0
            cmd = "select " + " ".join(shlex.quote(r) for r in refs)
            try:
                self._parawiz_execute_ccp(cmd, source="parawiz-select")
            except CommandError as exc:
                self.statusBar().showMessage(f"Selektion fehlgeschlagen: {exc}", 5000)
                return 0
            return len(refs)

        if not enforce_row_uniqueness:
            refs_bulk: list[str] = []
            for pid in pids:
                ref = self._parawiz_hash_name_for_parameter_id(pid)
                if ref:
                    refs_bulk.append(ref)
            if not refs_bulk:
                return 0
            try:
                self._parawiz_execute_ccp(
                    "select -p " + " ".join(shlex.quote(r) for r in refs_bulk),
                    source="parawiz-select",
                )
            except CommandError as exc:
                self.statusBar().showMessage(f"Selektion fehlgeschlagen: {exc}", 5000)
                return 0
            return len(refs_bulk)

        last_by_row: dict[int, UUID] = {}
        row_order: list[int] = []
        for pid in pids:
            row_i = self._parawiz_filtered_row_for_parameter(pid)
            if row_i is None:
                continue
            if row_i not in last_by_row:
                row_order.append(row_i)
            last_by_row[row_i] = pid
        effective = [last_by_row[r] for r in row_order]
        if not effective:
            return 0
        keep = set(effective)
        to_remove: list[UUID] = []
        seen_rm: set[str] = set()
        for r in row_order:
            for opid in self._parawiz_selected_pids_in_row(r):
                if opid in keep:
                    continue
                ps = str(opid)
                if ps not in seen_rm:
                    seen_rm.add(ps)
                    to_remove.append(opid)
        rm_refs = [r for pid in to_remove if (r := self._parawiz_hash_name_for_parameter_id(pid))]
        add_refs = [r for pid in effective if (r := self._parawiz_hash_name_for_parameter_id(pid))]
        if not add_refs:
            return 0
        try:
            if rm_refs:
                self._parawiz_execute_ccp(
                    "select -m " + " ".join(shlex.quote(r) for r in rm_refs),
                    source="parawiz-select",
                )
            self._parawiz_execute_ccp(
                "select -p " + " ".join(shlex.quote(r) for r in add_refs),
                source="parawiz-select",
            )
        except CommandError as exc:
            self.statusBar().showMessage(f"Selektion fehlgeschlagen: {exc}", 5000)
            return 0
        return len(add_refs)

    def _parawiz_select_filtered_dataset_parameters(self, ds_id: UUID, *, append: bool) -> int:
        rows = self._parawiz_filtered_rows_list()
        pids: list[UUID] = []
        for _name, by_ds in rows:
            hit = by_ds.get(ds_id)
            if hit is None:
                continue
            pids.append(hit[2])
        n = self._parawiz_select_parameter_ids(
            pids, append=append, enforce_row_uniqueness=False
        )
        mode = "addiert" if append else "gesetzt"
        if n > 0:
            self.statusBar().showMessage(f"Modell-Selektion {mode}: {n} Parameter.", 4500)
        else:
            self.statusBar().showMessage("Keine Parameter im aktuellen Filter für diesen Datensatz.", 4500)
        return n

    def _parawiz_selected_parameter_row_indices(self) -> set[int]:
        rows: set[int] = set()
        for tw in (self._table, self._table_frozen, self._table_target):
            sm = tw.selectionModel()
            if sm is None:
                continue
            idxs = sm.selectedIndexes()
            if idxs:
                rows.update(int(ix.row()) for ix in idxs)
            else:
                cur = sm.currentIndex()
                if cur.isValid():
                    rows.add(int(cur.row()))
        return rows

    def _parawiz_qt_row_indices_selection_only(self) -> set[int]:
        """Wie Qt-Zeilen, aber nur ``selectedIndexes`` — kein ``currentIndex``-Fallback (vermeidet Geisterzeile)."""
        rows: set[int] = set()
        for tw in (self._table, self._table_frozen, self._table_target):
            sm = tw.selectionModel()
            if sm is None:
                continue
            for ix in sm.selectedIndexes():
                rows.add(int(ix.row()))
        return rows

    def _parawiz_filtered_rows_matching_model_selection(self) -> set[int]:
        if not self._controller.selection:
            return set()
        pid_strs = self._parawiz_selected_pid_str_set()
        rows_it = self._parawiz_filtered_rows_list()
        out: set[int] = set()
        # Schneller Pfad: nur String-Vergleich — kein find_by_id pro Zelle (sonst ~10k× Kopieren „hängt“).
        if pid_strs:
            for i, (_name, by_ds) in enumerate(rows_it):
                for _hit in by_ds.values():
                    if str(_hit[2]) in pid_strs:
                        out.add(i)
                        break
            return out
        for i, (_name, by_ds) in enumerate(rows_it):
            for _hit in by_ds.values():
                opid = _hit[2]
                ref = self._parawiz_hash_name_for_parameter_id(opid)
                if self._parawiz_parameter_is_in_model_selection(opid, ref):
                    out.add(i)
                    break
        return out

    def _parawiz_copy_source_column_indices_for_row(
        self,
        tr: int,
        target_id: UUID,
        *,
        pid_set: set[str],
        rows_list: list[tuple[str, dict[UUID, tuple[str, str, UUID]]]],
    ) -> list[int]:
        """Alle gewählten Quell-Spalten dieser Zeile (ohne Ziel); mehrere Spalten = mehrere Quell-Kenngrößen.

        Primär: Spalten, deren Zell-Parameter-UUID in der Modell-Selektion liegt (unabhängig von Qt-Fokus /
        ``_parawiz_last_main_focus_col`` — die oft noch auf der ersten Vergleichsspalte steht).
        """
        datasets = self._cached_datasets
        cc = self._table.columnCount()
        if cc <= 0 or len(datasets) != cc:
            return []

        def _is_source_col(ci: int) -> bool:
            if ci < 0 or ci >= cc:
                return False
            return datasets[ci][1] != target_id

        if pid_set and 0 <= tr < len(rows_list):
            _name, by_ds = rows_list[tr]
            from_model: list[int] = []
            for i, (_dn, ds_id) in enumerate(datasets):
                if not _is_source_col(i):
                    continue
                hit = by_ds.get(ds_id)
                if hit is None:
                    continue
                if str(hit[2]) in pid_set:
                    from_model.append(i)
            if from_model:
                return sorted(from_model)

        sm = self._table.selectionModel()
        if sm is not None:
            cand = sorted(
                {
                    int(ix.column())
                    for ix in sm.selectedIndexes()
                    if ix.row() == tr and _is_source_col(int(ix.column()))
                }
            )
            if cand:
                return cand
            cur = sm.currentIndex()
            if cur.isValid() and cur.row() == tr and _is_source_col(int(cur.column())):
                return [int(cur.column())]
        fc = self._parawiz_last_main_focus_col
        if _is_source_col(fc):
            return [fc]
        return []

    def _parawiz_scratch_dataset_id(self) -> UUID | None:
        """UUID von ``parawiz_target``.

        Unabhängig von ``active_dataset_name`` (erste DCM-Registrierung setzt diese oft).
        """
        try:
            rt = self._controller.model.parameter_runtime()
            rt.ensure_tree()
            root = rt.data_sets_root()
            for c in root.children:
                if not isinstance(c, ComplexInstance) or c.id is None:
                    continue
                if c.name != PARAWIZ_TARGET_DATASET_NAME:
                    continue
                try:
                    if str(c.get("type")) == "MODEL.PARAMETER_DATA_SET":
                        return c.id
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _parawiz_ensure_target_scratch_dataset(self) -> None:
        """Leeren Zieldatensatz ``parawiz_target`` anlegen und in DuckDB registrieren.

        Direkt am Modellbaum, ohne CCP-``cd``.
        """
        rt = self._controller.model.parameter_runtime()
        rt.ensure_tree()
        ds_root = rt.data_sets_root()
        existing: ComplexInstance | None = None
        for c in ds_root.children:
            if isinstance(c, ComplexInstance) and c.name == PARAWIZ_TARGET_DATASET_NAME and c.id is not None:
                existing = c
                break
        if existing is None:
            node = ComplexInstance(name=PARAWIZ_TARGET_DATASET_NAME)
            self._controller.model.attach(node, parent=ds_root, reserve_existing=False, remap_ids=False)
            rt.register_data_set_node(
                node,
                source_path="",
                source_format="unknown",
                source_hash="",
            )
        else:
            rt.register_data_set_node(
                existing,
                source_path="",
                source_format="unknown",
                source_hash="",
            )
        try:
            rt.set_active_dataset_name(PARAWIZ_TARGET_DATASET_NAME)
        except ValueError:
            pass

    def _parawiz_copy_selection_to_target_dataset(self) -> None:
        """Kopiert die Selektion in den ParaWiz-Zieldatensatz (``parawiz_target``), nicht in die DCM-Vergleichssätze."""
        self._parawiz_ensure_target_scratch_dataset()
        sid = self._parawiz_scratch_dataset_id()
        if sid is None:
            self.statusBar().showMessage(
                "Kein Target-DataSet: parawiz_target nicht im Modell.",
                7000,
            )
            return
        target_ref = self._parawiz_dataset_hash_name(sid)
        if not target_ref:
            self.statusBar().showMessage("Kein Target-DataSet: Referenz konnte nicht aufgelöst werden.", 7000)
            return

        rows_list = self._parawiz_filtered_rows_list()
        datasets = self._cached_datasets
        ad_id_cp = sid
        sel_rows_sorted = sorted(self._parawiz_filtered_rows_matching_model_selection())
        pid_set_cp = self._parawiz_selected_pid_str_set()
        sel_refs: list[str] = []
        if (
            sel_rows_sorted
            and len(datasets) > 0
            and len(datasets) == self._table.columnCount()
        ):
            for tr in sel_rows_sorted:
                for sci in self._parawiz_copy_source_column_indices_for_row(
                    tr, ad_id_cp, pid_set=pid_set_cp, rows_list=rows_list
                ):
                    if not (0 <= sci < len(datasets)):
                        continue
                    _name, by_ds = rows_list[tr]
                    hit = by_ds.get(datasets[sci][1])
                    if hit is None:
                        continue
                    ref = self._parawiz_hash_name_for_parameter_id(hit[2])
                    if ref:
                        sel_refs.append(ref)
            _seen_ref: set[str] = set()
            sel_refs = [r for r in sel_refs if r not in _seen_ref and not _seen_ref.add(r)]
            if not sel_refs:
                self.statusBar().showMessage(
                    "Kopieren: Quell-Datensatz-Spalte für die Selektion nicht ermittelbar "
                    "(Zelle in der gewünschten Spalte fokussieren oder dort Auswahl setzen).",
                    8000,
                )
                return
        if not sel_refs:
            for obj in self._controller.selection:
                if not isinstance(obj, ComplexInstance) or obj.id is None:
                    continue
                if self._controller._node_model_type(obj) != "MODEL.CAL_PARAM":
                    continue
                sel_refs.append(obj.hash_name)
        if not sel_refs:
            self.statusBar().showMessage("Keine lila Modell-Selektion vorhanden.", 5000)
            return

        self._parawiz_copy_in_progress = True
        n_ok = 0
        n_skip = 0
        errs: list[str] = []
        skip_benign_lines: list[str] = []
        skip_other_lines: list[str] = []
        try:
            select_lines = _parawiz_build_ccp_select_lines(
                sel_refs, max_cmd_chars=self._PARAWIZ_CCP_CMD_MAX_TOTAL
            )
            for _si, _line in enumerate(select_lines):
                self._parawiz_execute_ccp(
                    _line,
                    source="parawiz-select",
                    refresh_selection_overlay=_si == len(select_lines) - 1,
                )
            out = self._parawiz_execute_ccp(
                f"cp @selection {shlex.quote(target_ref)}",
                source="parawiz",
                refresh_selection_overlay=False,
            )
            payload = json.loads(str(out or "{}"))
            n_ok = int(payload.get("copied", 0))
            n_skip = int(payload.get("skipped", 0))
            raw_errs = payload.get("errors", [])
            if isinstance(raw_errs, list):
                errs = [str(x) for x in raw_errs if str(x)]
            else:
                errs = [str(raw_errs)] if str(raw_errs) else []
            for _raw in payload.get("copied_dst_ids", []) or []:
                try:
                    _uid = UUID(str(_raw))
                    self._parawiz_target_db_copied_pids.add(_uid)
                except ValueError:
                    continue
            _repo_cp = self._controller.model.parameter_runtime().repo
            for _rs, _rd in zip(
                payload.get("copied_src_ids") or [],
                payload.get("copied_dst_ids") or [],
            ):
                try:
                    _suid = UUID(str(_rs))
                    _duid = UUID(str(_rd))
                except ValueError:
                    continue
                try:
                    _src_ds = _repo_cp.get_record(_suid).data_set_id
                except Exception:
                    continue
                _sci: int | None = None
                for _ci, (_na, _did) in enumerate(datasets):
                    if _did == _src_ds:
                        _sci = _ci
                        break
                if _sci is not None:
                    self._parawiz_target_copy_source_col_by_pid[_duid] = _sci
            _sd = payload.get("skipped_details")
            if isinstance(_sd, list):
                for _it in _sd:
                    if not isinstance(_it, dict):
                        continue
                    _nm = str(_it.get("name", "?"))
                    _rs = str(_it.get("reason", "?"))
                    _de = _PARAWIZ_CP_SKIP_REASON_DE.get(_rs, _rs)
                    _line = f"{_nm}: {_de}"
                    if _rs == "source_already_in_target_dataset":
                        skip_benign_lines.append(_line)
                    else:
                        skip_other_lines.append(_line)
        except (CommandError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errs = [str(exc)]
        finally:
            self._parawiz_copy_in_progress = False

        def _clear_sel_after_copy_layout() -> None:
            self._table.clearSelection()
            self._table_frozen.clearSelection()
            self._table_target.clearSelection()
            try:
                self._parawiz_execute_ccp(
                    "select",
                    source="parawiz-select",
                    refresh_selection_overlay=False,
                )
            except CommandError:
                pass
            self._parawiz_refresh_model_selection_overlay()

        self._parawiz_refresh_after_model_mutation(
            delete_progress=False,
            fit_window=False,
            on_layout_complete=_clear_sel_after_copy_layout,
        )
        parts = [f"{n_ok} kopiert", f"{n_skip} übersprungen", f"{len(errs)} Fehler"]
        _status_txt = " · ".join(parts)
        _status_ms = 8000
        if skip_benign_lines and not errs and not skip_other_lines:
            _status_txt += (
                " — "
                + "; ".join(skip_benign_lines[:3])
                + (" …" if len(skip_benign_lines) > 3 else "")
            )
            _status_ms = 10000
        self.statusBar().showMessage(_status_txt, _status_ms)
        dialog_chunks: list[str] = []
        if errs:
            dialog_chunks.append(
                "Beim Kopieren gab es Probleme:\n"
                + "\n".join(errs[:12])
                + ("\n…" if len(errs) > 12 else "")
            )
        if skip_other_lines:
            dialog_chunks.append(
                "Übersprungen:\n"
                + "\n".join(skip_other_lines[:24])
                + ("\n…" if len(skip_other_lines) > 24 else "")
            )
        if skip_benign_lines and (errs or skip_other_lines):
            dialog_chunks.append(
                "Hinweis (bereits im Ziel-Datensatz):\n"
                + "\n".join(skip_benign_lines[:24])
                + ("\n…" if len(skip_benign_lines) > 24 else "")
            )
        if dialog_chunks:
            QMessageBox.warning(self, "ParaWiz", "\n\n".join(dialog_chunks))

    def _parawiz_clear_model_selection(self) -> None:
        try:
            self._parawiz_execute_ccp("select", source="parawiz-select")
        except CommandError as exc:
            self.statusBar().showMessage(f"Selektion konnte nicht gelöscht werden: {exc}", 5000)
            return
        self._table.clearSelection()
        self._table_frozen.clearSelection()
        self._table_target.clearSelection()
        self.statusBar().showMessage("Modell-Selektion aufgehoben.", 3000)

    def _on_dataset_title_context_menu(self, w: QWidget, pos, ds_id: UUID) -> None:
        menu = QMenu(self)
        act_add = menu.addAction("Parameter dieses Datensatzes zur Selektion (additiv; erneut: entfernen)")
        act_replace = menu.addAction("Selektion ersetzen mit Datensatz-Parametern")
        act_clear = menu.addAction("Selektion aufheben")
        picked = menu.exec(w.mapToGlobal(pos))
        if picked is act_add:
            self._parawiz_select_filtered_dataset_parameters(ds_id, append=True)
        elif picked is act_replace:
            self._parawiz_select_filtered_dataset_parameters(ds_id, append=False)
        elif picked is act_clear:
            self._parawiz_clear_model_selection()

    def _open_parameter_cell_context_menu(self, gpos, pid: UUID | None) -> None:
        menu = QMenu(self)
        act_add = menu.addAction("Im Modell selektieren (Alt+Klick; erneut: entfernen)")
        act_replace = menu.addAction("Im Modell selektieren (ersetzen)")
        act_clear = menu.addAction("Selektion aufheben")
        if pid is None:
            act_add.setEnabled(False)
            act_replace.setEnabled(False)
        picked = menu.exec(gpos)
        if picked is act_add and pid is not None:
            self._parawiz_toggle_model_selection_for_parameter(pid)
        elif picked is act_replace and pid is not None:
            n = self._parawiz_select_parameter_ids([pid], append=False)
            if n:
                self.statusBar().showMessage("Modell-Selektion auf Parameter gesetzt.", 3500)
        elif picked is act_clear:
            self._parawiz_clear_model_selection()

    def _on_parameter_scroll_context_menu(self, pos) -> None:
        idx = self._table.indexAt(pos)
        row = int(idx.row()) if idx.isValid() else -1
        col = int(idx.column()) if idx.isValid() else -1
        pid = self._parawiz_pid_from_clicked_cell(row, col, from_frozen=False) if row >= 0 and col >= 0 else None
        self._open_parameter_cell_context_menu(self._table.viewport().mapToGlobal(pos), pid)

    def _on_parameter_frozen_context_menu(self, pos) -> None:
        idx = self._table_frozen.indexAt(pos)
        row = int(idx.row()) if idx.isValid() else -1
        pid = self._parawiz_pid_for_frozen_row_model_action(row) if row >= 0 else None
        self._open_parameter_cell_context_menu(self._table_frozen.viewport().mapToGlobal(pos), pid)

    def _on_parameter_target_context_menu(self, pos) -> None:
        idx = self._table_target.indexAt(pos)
        row = int(idx.row()) if idx.isValid() else -1
        pid = (
            self._parawiz_pid_from_clicked_cell(row, 0, from_target=True)
            if row >= 0
            else None
        )
        self._open_parameter_cell_context_menu(self._table_target.viewport().mapToGlobal(pos), pid)

    def _on_parameter_frozen_table_clicked(self, row: int, col: int) -> None:
        _ = col
        self._on_parameter_compare_gesture(row, col=0, from_frozen=True)

    def _on_parameter_scroll_table_clicked(self, row: int, col: int) -> None:
        self._on_parameter_compare_gesture(row, col=col, from_frozen=False)

    def _on_parameter_compare_gesture(
        self, row: int, col: int, *, from_frozen: bool, from_target: bool = False
    ) -> None:
        """Erste Zelle ohne Modifikator, zweite mit Strg: Parametervergleich."""
        mods = QApplication.keyboardModifiers()
        if bool(mods & Qt.KeyboardModifier.AltModifier):
            if from_frozen:
                pid = self._parawiz_pid_for_frozen_row_model_action(row)
            else:
                pid = self._parawiz_pid_from_clicked_cell(
                    row, col, from_frozen=False, from_target=from_target
                )
            if pid is None:
                return
            self._parawiz_toggle_model_selection_for_parameter(pid)
            return
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        pid = self._parawiz_pid_from_clicked_cell(
            row, col, from_frozen=from_frozen, from_target=from_target
        )
        if pid is None:
            if not ctrl:
                self._parawiz_compare_first_pid = None
                self._parawiz_compare_first_row = None
                self._parawiz_compare_first_from_target = False
            return
        if ctrl:
            if self._parawiz_compare_first_pid is None:
                self.statusBar().showMessage(
                    "Vergleich: Zuerst die erste Zelle ohne Strg anklicken, dann die zweite mit Strg+Klick.",
                    5000,
                )
                return
            first_pid = self._parawiz_compare_first_pid
            first_row = self._parawiz_compare_first_row
            first_from_target = bool(self._parawiz_compare_first_from_target)
            self._parawiz_compare_first_pid = None
            self._parawiz_compare_first_row = None
            self._parawiz_compare_first_from_target = False
            if first_pid == pid:
                alt = self._parawiz_alternate_cal_param_pid_same_row(first_row, pid)
                if alt is not None:
                    self._open_calibration_map_compare_dialog(
                        first_pid,
                        alt,
                        first_from_target=(first_from_target or from_target),
                        second_from_target=False,
                    )
                else:
                    self.statusBar().showMessage(
                        "Vergleich: Dieselbe Kenngröße (UUID) — bitte zwei verschiedene "
                        "Parametersatz-Spalten oder Zeilen wählen.",
                        5000,
                    )
                return
            self._open_calibration_map_compare_dialog(
                first_pid,
                pid,
                first_from_target=first_from_target,
                second_from_target=from_target,
            )
            return
        self._parawiz_compare_first_pid = pid
        self._parawiz_compare_first_row = row
        self._parawiz_compare_first_from_target = bool(from_target)
        self.statusBar().showMessage(
            "Vergleich: Erste Zelle markiert. Zweite Zelle mit Strg+Klick wählen (Skalar oder Kennlinie/Kennfeld).",
            5000,
        )

    def _parawiz_alternate_cal_param_pid_same_row(self, row: int | None, pid: UUID) -> UUID | None:
        """Gleiche UUID z. B. Target-Spalte und Quellspalte: der andere Satz in derselben Zeile."""
        if row is None or row < 0:
            return None
        rows_list = self._parawiz_filtered_rows_list()
        if row >= len(rows_list):
            return None
        _name, by_ds = rows_list[row]
        siblings = [hit[2] for hit in by_ds.values() if hit[2] != pid]
        if len(siblings) != 1:
            return None
        return siblings[0]

    def _open_calibration_map_compare_dialog(
        self,
        pid_a: UUID,
        pid_b: UUID,
        *,
        first_from_target: bool = False,
        second_from_target: bool = False,
    ) -> None:
        from synariustools.tools.calmapwidget import (
            CalibrationMapData,
            build_scalar_calibration_readonly_widget,
            create_calibration_map_compare_viewer,
            supports_calibration_plot,
            supports_calibration_scalar_edit,
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

        ds_a = repo.get_dataset_init_file_stem(rec_a.data_set_id)
        ds_b = repo.get_dataset_init_file_stem(rec_b.data_set_id)
        if first_from_target:
            ds_a = f"Target ({ds_a})"
        if second_from_target:
            ds_b = f"Target ({ds_b})"

        scalar_a = supports_calibration_scalar_edit(rec_a)
        scalar_b = supports_calibration_scalar_edit(rec_b)
        if scalar_a and scalar_b:
            d_a = CalibrationMapData.from_parameter_record(rec_a)
            d_b = CalibrationMapData.from_parameter_record(rec_b)
            dlg = QDialog(self)
            dlg.setWindowTitle(f"ParaWiz — Vergleich {rec_a.name}")
            dlg.setWindowIcon(self._app_icon)
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(8, 8, 8, 8)
            lay.setSpacing(0)
            tabs = QTabWidget(dlg)
            tabs.addTab(build_scalar_calibration_readonly_widget(tabs, d_a), ds_a)
            tabs.addTab(build_scalar_calibration_readonly_widget(tabs, d_b), ds_b)
            lay.addWidget(tabs)
            dlg.resize(max(480, dlg.sizeHint().width()), max(440, dlg.sizeHint().height()))
            self._register_modeless_param_viewer(dlg)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            return

        if scalar_a or scalar_b:
            QMessageBox.information(
                self,
                "ParaWiz",
                "Der Vergleich ist nur für zwei Skalare oder für zwei Kennlinien/Kennfelder "
                "mit gleicher Dimension möglich.",
            )
            return

        if not supports_calibration_plot(rec_a) or not supports_calibration_plot(rec_b):
            QMessageBox.information(
                self,
                "ParaWiz",
                "Vergleichsmodus ist nur für numerische Skalare, Kennlinien und Kennfelder verfügbar.",
            )
            return
        va = np.asarray(rec_a.values, dtype=np.float64)
        vb = np.asarray(rec_b.values, dtype=np.float64)
        if va.ndim not in (1, 2) or vb.ndim not in (1, 2):
            QMessageBox.information(
                self,
                "ParaWiz",
                "Vergleichsmodus unterstützt numerische Kennlinien (1D) und Kennfelder (2D).",
            )
            return
        d_a = CalibrationMapData.from_parameter_record(rec_a)
        d_b = CalibrationMapData.from_parameter_record(rec_b)

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
        dlg.resize(max(320, sh.width()), max(700, sh.height()))
        self._register_modeless_param_viewer(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_parameter_table_double_clicked_impl(
        self, row: int, col: int, *, from_frozen: bool = False, from_target: bool = False
    ) -> None:
        # Mit SelectRows liefert Qt die Doppelklick-Spalte u. U. als 0 statt der angeklickten Spalte —
        # daher jede Spalte der Zeile zulassen.
        pid = self._parawiz_pid_from_clicked_cell(
            row, col, from_frozen=from_frozen, from_target=from_target
        )
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
