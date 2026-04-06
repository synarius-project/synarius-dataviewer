"""Editable QTableWidget for calibration maps: selection rules, bulk ops keys, digit wheel."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QFontMetrics, QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyleOptionViewItem, QTableWidget, QTableWidgetItem

# #region agent log
_EDIT_AGENT_LOG = Path(__file__).resolve().parents[5] / "debug-85e82c.log"


def _edit_agent_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "85e82c",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(_EDIT_AGENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _qt_int_for_log(v: object) -> int:
    """Qt Key / KeyboardModifiers: PySide6 kann Enum/QFlags liefern — nicht immer ``int(v)``."""
    if isinstance(v, int):
        return v
    u = getattr(v, "value", v)
    if isinstance(u, int):
        return u
    try:
        return int(u)
    except (TypeError, ValueError):
        return -1


# #endregion

# Tastatur layout-unabhängig: erzeugtes Zeichen (z. B. DE: Shift+0 → "="), nicht nur Qt.Key_*.
_BULK_OP_BY_TEXT: dict[str, str] = {
    "=": "=",
    "+": "+",
    "-": "-",
    "*": "*",
    "/": "/",
}


class CalmapEditorHost(Protocol):
    """Callbacks from :class:`EditableCalmapTable` into :class:`CalibrationMapWidget`."""

    def editor_cell_kind(self, row: int, col: int) -> str: ...

    def editor_handle_bulk_operator(self, op: str) -> None: ...

    def editor_begin_cell_edit(self, row: int, col: int) -> None: ...

    def editor_wheel_digit(self, row: int, col: int, digit_index: int, delta: int) -> None: ...

    def editor_digit_index_at(self, row: int, col: int, pos_in_cell: QPoint) -> int | None: ...

    def editor_has_numeric_selection(self) -> bool: ...

    def editor_selection_is_homogeneous_numeric(self) -> bool: ...


class EditableCalmapTable(QTableWidget):
    """Rubber-band / Ctrl multi-select; keys + - * / = (set all); digit wheel."""

    def __init__(self, host: CalmapEditorHost, parent=None) -> None:
        super().__init__(parent)
        self._host = host
        self._filtering_selection = False
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.cellDoubleClicked.connect(self._on_cell_double_clicked)
        # #region agent log
        _dd, _sm, _sb = self.dragDropMode(), self.selectionMode(), self.selectionBehavior()
        _edit_agent_log(
            "D",
            "edit_table.__init__",
            "table config",
            {
                "dragDropMode": getattr(_dd, "value", str(_dd)),
                "selectionMode": getattr(_sm, "value", str(_sm)),
                "selBehavior": getattr(_sb, "value", str(_sb)),
            },
        )
        # #endregion

    def _on_selection_changed(self, *_args: object) -> None:
        if self._filtering_selection:
            return
        self._filtering_selection = True
        try:
            sm = self.selectionModel()
            idxs = [ix for ix in sm.selectedIndexes() if ix.isValid()]
            if not idxs:
                return
            cur = sm.currentIndex()
            wanted_cur = self._host.editor_cell_kind(cur.row(), cur.column()) if cur.isValid() else "invalid"
            wanted = self._host.editor_cell_kind(idxs[0].row(), idxs[0].column())
            # #region agent log
            _edit_agent_log(
                "C",
                "edit_table._on_selection_changed",
                "selection from first index",
                {
                    "n_sel": len(idxs),
                    "cur_rc": [cur.row(), cur.column()] if cur.isValid() else None,
                    "wanted_cur": wanted_cur,
                    "wanted_first": wanted,
                    "first_rc": [idxs[0].row(), idxs[0].column()],
                },
            )
            # #endregion
            if wanted not in ("value", "axis_x", "axis_y", "scalar"):
                # #region agent log
                _edit_agent_log(
                    "E",
                    "edit_table._on_selection_changed",
                    "clearSelection bad first kind",
                    {"wanted": wanted, "n_sel": len(idxs)},
                )
                # #endregion
                sm.clearSelection()
                return
            for ix in list(idxs):
                if not ix.isValid():
                    continue
                k = self._host.editor_cell_kind(ix.row(), ix.column())
                if k != wanted:
                    sm.select(ix, QItemSelectionModel.SelectionFlag.Deselect)
        finally:
            self._filtering_selection = False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._host.editor_has_numeric_selection():
            k = event.key()
            mods = event.modifiers()
            if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                super().keyPressEvent(event)
                return
            ch = event.text()
            if len(ch) == 1 and ch in _BULK_OP_BY_TEXT:
                op = _BULK_OP_BY_TEXT[ch]
                # #region agent log
                _edit_agent_log(
                    "EQ_CHAR",
                    "edit_table.keyPressEvent",
                    "bulk op via event.text()",
                    {
                        "text": ch,
                        "op": op,
                        "key": _qt_int_for_log(k),
                        "mods": _qt_int_for_log(mods),
                    },
                )
                # #endregion
                self._host.editor_handle_bulk_operator(op)
                event.accept()
                return
            if k == Qt.Key.Key_Equal:
                if mods & Qt.KeyboardModifier.ShiftModifier:
                    self._host.editor_handle_bulk_operator("+")
                else:
                    self._host.editor_handle_bulk_operator("=")
                event.accept()
                return
            if k == Qt.Key.Key_Plus:
                self._host.editor_handle_bulk_operator("+")
                event.accept()
                return
            if k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
                self._host.editor_handle_bulk_operator("-")
                event.accept()
                return
            if k in (Qt.Key.Key_Asterisk, Qt.Key.Key_8) and (mods & Qt.KeyboardModifier.ShiftModifier):
                self._host.editor_handle_bulk_operator("*")
                event.accept()
                return
            if k == Qt.Key.Key_Asterisk:
                self._host.editor_handle_bulk_operator("*")
                event.accept()
                return
            if k == Qt.Key.Key_Slash:
                self._host.editor_handle_bulk_operator("/")
                event.accept()
                return
        super().keyPressEvent(event)

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        self._host.editor_begin_cell_edit(row, col)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if (
            not self._host.editor_selection_is_homogeneous_numeric()
            or not self._host.editor_has_numeric_selection()
        ):
            super().wheelEvent(event)
            return
        pos_vp = self.viewport().mapFrom(self, event.position().toPoint())
        ix = self.indexAt(pos_vp)
        if not ix.isValid():
            super().wheelEvent(event)
            return
        if not self.selectionModel().isSelected(ix):
            super().wheelEvent(event)
            return
        rect = self.visualRect(ix)
        local = pos_vp - rect.topLeft()
        d_idx = self._host.editor_digit_index_at(ix.row(), ix.column(), local)
        if d_idx is None:
            super().wheelEvent(event)
            return
        dy = event.angleDelta().y()
        if dy == 0:
            super().wheelEvent(event)
            return
        delta = 1 if dy > 0 else -1
        self._host.editor_wheel_digit(ix.row(), ix.column(), d_idx, delta)
        event.accept()


def digit_index_at_cell_pos(item: QTableWidgetItem | None, pos_in_cell: QPoint, table: QTableWidget) -> int | None:
    """Return index into ``item.text()`` for the digit under ``pos_in_cell``, or None."""
    if item is None:
        return None
    text = item.text()
    if not text:
        return None
    opt = QStyleOptionViewItem()
    opt.initFrom(table)
    opt.font = item.font()
    fm = QFontMetrics(opt.font)
    opt.rect = table.visualItemRect(item)
    opt.text = text
    opt.displayAlignment = int(item.textAlignment())
    # Left/center padding similar to QCommonStyle
    margin = table.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, opt, table) + 1
    x = pos_in_cell.x() - margin
    if x < 0:
        x = 0
    # Find character boundary
    acc = 0
    for i, ch in enumerate(text):
        w = fm.horizontalAdvance(ch)
        if acc + w / 2 >= x:
            if ch.isdigit():
                return i
            return None
        acc += w
    last = len(text) - 1
    if last >= 0 and text[last].isdigit():
        return last
    return None


def adjust_digit_in_numeric_string(text: str, digit_index: int, delta: int) -> str | None:
    """Increment/decrement the digit at ``digit_index`` with carry within the mantissa; returns new text or None."""
    if digit_index < 0 or digit_index >= len(text) or not text[digit_index].isdigit():
        return None
    chars = list(text)

    if delta > 0:
        j = digit_index
        while j >= 0:
            if not chars[j].isdigit():
                j -= 1
                continue
            n = int(chars[j]) + 1
            if n <= 9:
                chars[j] = str(n)
                return "".join(chars)
            chars[j] = "0"
            j -= 1
        return "1" + "".join(chars)
    if delta < 0:
        j = digit_index
        while j >= 0:
            if not chars[j].isdigit():
                j -= 1
                continue
            n = int(chars[j]) - 1
            if n >= 0:
                chars[j] = str(n)
                return "".join(chars)
            chars[j] = "9"
            j -= 1
        # underflow at MSB -> clamp to 0 for simplicity
        return None
    return None
