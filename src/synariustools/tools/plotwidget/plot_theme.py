"""QSS for plot toolbar and legend (standalone copy of Synarius Studio chrome subset)."""

from __future__ import annotations

from PySide6.QtGui import QColor


def _rgb_hex_scale(hex_rgb: str, factor: float) -> str:
    s = hex_rgb.strip().removeprefix("#")
    if len(s) != 6:
        raise ValueError(f"expected #RRGGBB, got {hex_rgb!r}")
    r, g, b = (int(s[i : i + 2], 16) for i in (0, 2, 4))
    r = max(0, min(255, int(round(r * factor))))
    g = max(0, min(255, int(round(g * factor))))
    b = max(0, min(255, int(round(b * factor))))
    return f"#{r:02x}{g:02x}{b:02x}"


RESOURCES_PANEL_BACKGROUND = "#c8e3fb"
RESOURCES_PANEL_ALTERNATE_ROW = _rgb_hex_scale(RESOURCES_PANEL_BACKGROUND, 0.90)

STUDIO_TOOLBAR_BACKGROUND = "#000000"
STUDIO_TOOLBAR_FOREGROUND = "#ffffff"
STUDIO_TOOLBAR_HOVER = "#2a2a2a"
STUDIO_TOOLBAR_COMBO_BACKGROUND = "#333333"
STUDIO_TOOLBAR_COMBO_BORDER = "#555555"
STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND = "#586cd4"
STUDIO_TOOLBAR_ACTION_HOVER = _rgb_hex_scale(STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND, 0.40)
STUDIO_TOOLBAR_ACTION_PRESSED = _rgb_hex_scale(STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND, 0.72)


def studio_toolbar_stylesheet() -> str:
    bg = STUDIO_TOOLBAR_BACKGROUND
    fg = STUDIO_TOOLBAR_FOREGROUND
    combo_hover = STUDIO_TOOLBAR_HOVER
    combo_bg = STUDIO_TOOLBAR_COMBO_BACKGROUND
    tb_hover = STUDIO_TOOLBAR_ACTION_HOVER
    tb_pressed = STUDIO_TOOLBAR_ACTION_PRESSED
    action_checked = STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND
    bdr = STUDIO_TOOLBAR_COMBO_BORDER
    return (
        f"QToolBar {{ background-color: {bg}; border: none; padding: 3px; spacing: 4px; }}"
        f"QToolBar QLabel {{ color: {fg}; }}"
        f"QToolBar QToolButton {{ background-color: {bg}; border: none; border-radius: 4px; padding: 4px; }}"
        f"QToolBar QToolButton:hover {{ background-color: {tb_hover}; }}"
        f"QToolBar QToolButton:pressed {{ background-color: {tb_pressed}; }}"
        f"QToolBar QToolButton:checked {{ background-color: {action_checked}; }}"
        f"QToolBar QComboBox {{ color: {fg}; background-color: {combo_bg}; border: 1px solid {bdr};"
        f" border-radius: 3px; padding: 2px 8px; min-height: 20px; }}"
        f"QToolBar QComboBox:hover {{ background-color: {combo_hover}; }}"
        f"QToolBar QComboBox::drop-down {{ border: none; width: 18px; }}"
        f"QToolBar QComboBox QAbstractItemView {{ background-color: {combo_bg}; color: {fg}; }}"
        f"QToolBar QLineEdit {{ color: {fg}; background-color: transparent; border: none; }}"
    )


def _scoped_channel_grid_table_qss(scope: str) -> str:
    bg = RESOURCES_PANEL_BACKGROUND
    alt = RESOURCES_PANEL_ALTERNATE_ROW
    hdr_bg = "#353535"
    hdr_fg = "#ffffff"
    return (
        f"{scope} QTableWidget {{"
        f" background-color: {bg};"
        f" alternate-background-color: {alt};"
        f" color: #1a1a1a;"
        f" gridline-color: transparent;"
        f" border: none;"
        f" font-size: 11px;"
        f"}}"
        f"{scope} QTableWidget::item {{ padding: 0px 2px; }}"
        f"{scope} QTableWidget::item:selected {{"
        f" background-color: #586cd4;"
        f" color: #ffffff;"
        f"}}"
        f"{scope} QHeaderView::section {{"
        f" background-color: {hdr_bg};"
        f" color: {hdr_fg};"
        f" padding: 2px 4px;"
        f" border: none;"
        f" font-size: 11px;"
        f"}}"
        f"{scope} QScrollBar:vertical {{ background: #2f2f2f; width: 12px; margin: 0; border: none; }}"
        f"{scope} QScrollBar::handle:vertical {{ background: #5a5a5a; min-height: 20px; border-radius: 4px; }}"
        f"{scope} QScrollBar::handle:vertical:hover {{ background: #6a6a6a; }}"
        f"{scope} QScrollBar::add-line:vertical, {scope} QScrollBar::sub-line:vertical "
        f"{{ height: 0; border: none; background: none; }}"
        f"{scope} QScrollBar::add-page:vertical, {scope} QScrollBar::sub-page:vertical {{ background: #2f2f2f; }}"
        f"{scope} QScrollBar:horizontal {{ background: #2f2f2f; height: 12px; margin: 0; border: none; }}"
        f"{scope} QScrollBar::handle:horizontal {{ background: #5a5a5a; min-width: 20px; border-radius: 4px; }}"
        f"{scope} QScrollBar::handle:horizontal:hover {{ background: #6a6a6a; }}"
        f"{scope} QScrollBar::add-line:horizontal, {scope} QScrollBar::sub-line:horizontal "
        f"{{ width: 0; border: none; background: none; }}"
        f"{scope} QScrollBar::add-page:horizontal, {scope} QScrollBar::sub-page:horizontal {{ background: #2f2f2f; }}"
    )


def data_viewer_legend_panel_stylesheet() -> str:
    bg = RESOURCES_PANEL_BACKGROUND
    scope = "QWidget#LegendPanel"
    return f"{scope} {{ background-color: {bg}; }}" + _scoped_channel_grid_table_qss(scope)


def selection_highlight_qcolor(*, opaque: bool = True) -> QColor:
    c = QColor(STUDIO_TOOLBAR_ACTIVE_ACTION_BACKGROUND)
    c.setAlpha(255 if opaque else 142)
    return c
