"""Status bar message area with a QProgressBar as background."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QProgressBar, QSizePolicy, QWidget


class StatusMessageProgressBar(QWidget):
    """Shows status text over a progress bar (chunk color = active toolbar accent)."""

    def __init__(
        self,
        *,
        accent_color: str,
        bar_width: int = 340,
        bar_height: int = 13,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._bar_height = max(10, bar_height)
        self.setFixedWidth(bar_width)
        self.setFixedHeight(self._bar_height + 2)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._bar = QProgressBar(self)
        self._bar.setTextVisible(False)
        self._accent = accent_color
        self._apply_bar_style()
        self._label = QLabel(self)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignLeading
        )
        self._label.setWordWrap(False)
        # Legible on light track and on blue chunk (QSSE has limited shadow support)
        self._label.setStyleSheet(
            "QLabel { background: transparent; color: #141414; padding-left: 6px; padding-right: 4px; "
            "font-size: 11px; font-weight: 600; }"
        )
        self._full_text = ""

    def _apply_bar_style(self) -> None:
        a = self._accent
        h = self._bar_height
        self._bar.setStyleSheet(
            "QProgressBar {"
            " border: 1px solid #888888;"
            " border-radius: 2px;"
            " background-color: #e8e8e8;"
            " margin: 0px;"
            f" min-height: {h}px;"
            f" max-height: {h}px;"
            "}"
            f"QProgressBar::chunk {{ background-color: {a}; border-radius: 1px; margin: 0px; }}"
        )

    def set_accent_color(self, color: str) -> None:
        if color == self._accent:
            return
        self._accent = color
        self._apply_bar_style()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        r = self.rect()
        bh = self._bar_height
        y = max(0, (r.height() - bh) // 2)
        self._bar.setGeometry(0, y, r.width(), bh)
        self._label.setGeometry(r)
        self._label.raise_()
        self._refresh_label_elide()

    def _refresh_label_elide(self) -> None:
        if not self._full_text:
            self._label.setText("")
            return
        fm = QFontMetrics(self._label.font())
        w = max(8, self.width() - 12)
        self._label.setText(fm.elidedText(self._full_text, Qt.TextElideMode.ElideRight, w))

    def set_message(self, text: str) -> None:
        self._full_text = text
        self._refresh_label_elide()

    def set_range(self, minimum: int, maximum: int) -> None:
        self._bar.setRange(minimum, maximum)

    def set_value(self, value: int) -> None:
        self._bar.setValue(value)

    def value(self) -> int:
        return self._bar.value()
