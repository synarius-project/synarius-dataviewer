"""Low-level time-series scope: QWidget + QPixmap + QPainter.

Avoids QGraphicsScene / pyqtgraph for the plot area so slider drags only repaint one pixmap and
emit a lightweight signal — suitable for real-time updates.
"""

from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFontMetrics,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

_MAX_DRAW_POINTS = 12_000
# Ignore extreme tails when fitting axes (outlier channels / bad timestamps).
_PCT_LO = 0.5
_PCT_HI = 99.5
_POOL_MAX = 120_000

# Interaction
_ZOOM_WHEEL_FACTOR = 0.86
_AXIS_WHEEL_STRIP_PX = 44
_SLIDER_LINE_WIDTH = 3
_SLIDER_CIRCLE_R = 9


class PixmapScopeWidget(QWidget):
    """Black scope with dashed grid, white plot border, polylines, and optional sliders on top."""

    slider_positions_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._margin_left = 12
        # Slightly larger gap to the right widget edge.
        self._margin_right = 24
        self._margin_bottom = 40
        self._margin_top = 28
        self._channels: OrderedDict[str, dict[str, object]] = OrderedDict()
        self.min_x = 0.0
        self.max_x = 1.0
        self.min_y = 0.0
        self.max_y = 1.0
        self._num_xticks = 6
        self._num_yticks = 6
        self._walking = False
        self._walk_span = 10.0

        self.checkslider = False
        # Slider positions in data (x) units so they stay fixed when the x-axis is rescaled.
        self._slider_x_a: float | None = None
        self._slider_x_b: float | None = None
        self.flag_a = True
        self.flag_b = True
        self._slider_hit_a = QRect()
        self._slider_hit_b = QRect()

        self._pixmap = QPixmap()
        self._pen_dash = QPen(QColor(180, 180, 180), 1, Qt.PenStyle.DashLine)
        self._pen_axis = QPen(QColor(255, 255, 255), 1)
        self._color_slider_a = QColor(255, 204, 0)
        self._color_slider_b = QColor(255, 102, 255)
        self._pen_slider_a = QPen(self._color_slider_a, _SLIDER_LINE_WIDTH, Qt.PenStyle.SolidLine)
        self._pen_slider_a.setCosmetic(True)
        self._pen_slider_b = QPen(self._color_slider_b, _SLIDER_LINE_WIDTH, Qt.PenStyle.SolidLine)
        self._pen_slider_b.setCosmetic(True)

        self._rubber_active = False
        self._rubber_rect = QRect()
        self._panning = False
        self._pan_last: QPoint | None = None

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(0, self._margin_top + self._margin_bottom)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setStyleSheet("background-color: #2f2f2f;")
        self.setAutoFillBackground(True)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._margin_top + self._margin_bottom)

    def sizeHint(self) -> QSize:
        m = max(self._margin_left, self._margin_right, self._margin_bottom, 50)
        return QSize(12 * m, 8 * m)

    def set_series(self, name: str, t: np.ndarray, y: np.ndarray, pen: QPen) -> None:
        prev = self._channels.get(name)
        visible = True if prev is None else bool(prev.get("visible", True))
        self._channels[name] = {
            "t": np.asarray(t, dtype=np.float64).ravel(),
            "y": np.asarray(y, dtype=np.float64).ravel(),
            "pen": pen,
            "visible": visible,
        }
        self._apply_walk_or_refresh()

    def set_series_visible(self, name: str, visible: bool) -> None:
        ch = self._channels.get(name)
        if ch is None:
            return
        ch["visible"] = bool(visible)
        self._apply_walk_or_refresh()

    def is_series_visible(self, name: str) -> bool:
        ch = self._channels.get(name)
        if ch is None:
            return True
        return bool(ch.get("visible", True))

    def remove_series(self, name: str) -> None:
        self._channels.pop(name, None)
        if self._channels:
            self._apply_walk_or_refresh()
        else:
            self.min_x = 0.0
            self.max_x = 1.0
            self.min_y = 0.0
            self.max_y = 1.0
            self._slider_x_a = None
            self._slider_x_b = None
            self.refresh_pixmap()

    def clear_series(self) -> None:
        self._channels.clear()
        self.min_x = 0.0
        self.max_x = 1.0
        self.min_y = 0.0
        self.max_y = 1.0
        self._slider_x_a = None
        self._slider_x_b = None
        self.refresh_pixmap()

    def series_names(self) -> list[str]:
        return list(self._channels.keys())

    def get_series(self, name: str) -> tuple[np.ndarray, np.ndarray] | None:
        ch = self._channels.get(name)
        if ch is None:
            return None
        return ch["t"], ch["y"]  # type: ignore[return-value]

    def set_walking_axis(self, enabled: bool, span: float = 10.0) -> None:
        self._walking = enabled
        self._walk_span = max(span, 1e-9)
        self._apply_walk_or_refresh()

    def _pooled_finite(self, key: str, max_points: int = _POOL_MAX) -> np.ndarray:
        visible_ch = [ch for ch in self._channels.values() if ch.get("visible", True)]
        n_ch = len(visible_ch)
        per = max(512, max_points // max(1, n_ch))
        blocks: list[np.ndarray] = []
        for ch in visible_ch:
            a = np.asarray(ch[key], dtype=np.float64).ravel()
            m = np.isfinite(a)
            a = a[m]
            if a.size == 0:
                continue
            if a.size > per:
                idx = np.linspace(0, a.size - 1, per, dtype=np.int64)
                a = a[idx]
            blocks.append(a)
        if not blocks:
            return np.array([], dtype=np.float64)
        return np.concatenate(blocks)

    @staticmethod
    def _percentile_span(a: np.ndarray, lo_pct: float, hi_pct: float) -> tuple[float, float] | None:
        a = np.asarray(a, dtype=np.float64).ravel()
        m = np.isfinite(a)
        a = a[m]
        if a.size == 0:
            return None
        if a.size <= 4:
            lo_v, hi_v = float(np.min(a)), float(np.max(a))
        else:
            lo_v = float(np.percentile(a, lo_pct))
            hi_v = float(np.percentile(a, hi_pct))
        if not (math.isfinite(lo_v) and math.isfinite(hi_v)):
            return None
        if hi_v <= lo_v:
            hi_v = lo_v + 1e-9 * max(1.0, abs(lo_v))
        return lo_v, hi_v

    @staticmethod
    def _nice_ticks(
        vmin: float,
        vmax: float,
        n: int,
    ) -> tuple[float, float, int]:
        """Return (start, step, count) for 'nice' tick values covering [vmin, vmax]."""
        if not (math.isfinite(vmin) and math.isfinite(vmax)) or vmax <= vmin:
            return 0.0, 1.0, 2
        span = vmax - vmin
        raw_step = span / max(n, 1)
        if raw_step <= 0:
            raw_step = 1.0
        exp = math.floor(math.log10(raw_step))
        base = 10**exp
        for m in (1.0, 2.0, 5.0, 10.0):
            step = m * base
            if step >= raw_step * 0.999:
                break
        start = math.floor(vmin / step) * step
        count = int(math.floor((vmax - start) / step) + 1)
        count = max(count, 2)
        return start, step, count

    def _update_left_margin_for_labels(self) -> None:
        """Ensure left margin is wide enough so Y-axis labels are fully visible."""
        fm = QFontMetrics(self.font())
        y0, ystep, ny = self._nice_ticks(self.min_y, self.max_y, self._num_yticks)
        max_w = 0
        for j in range(ny):
            yv = y0 + j * ystep
            text = f"{yv:.6g}"
            max_w = max(max_w, fm.horizontalAdvance(text))
        # Keep labels visible and avoid touching/overlapping the plot border.
        needed = max(26, max_w + 8)
        if needed > self._margin_left:
            self._margin_left = needed

    def _median_channel_t_end(self) -> float | None:
        ends: list[float] = []
        for ch in self._channels.values():
            if not ch.get("visible", True):
                continue
            t = np.asarray(ch["t"], dtype=np.float64).ravel()
            m = np.isfinite(t)
            t = t[m]
            if t.size == 0:
                continue
            ends.append(float(np.max(t)))
        if not ends:
            return None
        return float(np.median(np.asarray(ends, dtype=np.float64)))

    @staticmethod
    def _seconds_to_data_units_factor(t_pool: np.ndarray) -> float:
        m = np.isfinite(t_pool)
        a = t_pool[m]
        if a.size == 0:
            return 1.0
        med = float(np.median(np.abs(a)))
        if med > 1e14:
            return 1e9
        if med > 1e11:
            return 1e6
        if med > 1e8:
            return 1e3
        return 1.0

    def _walk_window_width_data_units(self, t_pool: np.ndarray) -> float:
        base = self._walk_span * self._seconds_to_data_units_factor(t_pool)
        if t_pool.size >= 2:
            lo_p = float(np.percentile(t_pool, 0.5))
            hi_p = float(np.percentile(t_pool, 99.5))
            span = max(hi_p - lo_p, 0.0)
            if span > 0 and base < span * 1e-6:
                base = max(span * 0.12, base * (span / max(base, 1e-300)) * 0.05)
        return max(base, 1e-30)

    def _apply_padded_axis_limits(
        self,
        *,
        x_span: tuple[float, float] | None,
        y_span: tuple[float, float] | None,
        walking_t_ref: float | None,
        t_pool: np.ndarray | None = None,
    ) -> None:
        if y_span is None:
            return
        y_lo, y_hi = y_span
        span_y = y_hi - y_lo
        pad_y = 0.02 * span_y if span_y > 0 else 0.02
        self.min_y = y_lo - pad_y
        self.max_y = y_hi + pad_y
        if self.max_y <= self.min_y:
            self.max_y = self.min_y + 1e-9

        if self._walking and walking_t_ref is not None and math.isfinite(walking_t_ref):
            tw = self._walk_span
            if t_pool is not None and t_pool.size > 0:
                tw = self._walk_window_width_data_units(t_pool)
            self.max_x = walking_t_ref
            self.min_x = walking_t_ref - tw
        elif x_span is not None:
            x_lo, x_hi = x_span
            span_x = x_hi - x_lo
            pad_x = 0.02 * span_x if span_x > 0 else 0.02
            self.min_x = x_lo - pad_x
            self.max_x = x_hi + pad_x
            if self.max_x <= self.min_x:
                self.max_x = self.min_x + 1e-9

    def auto_range(self) -> None:
        if not self._channels:
            return
        t_pool = self._pooled_finite("t")
        y_pool = self._pooled_finite("y")
        x_sp = self._percentile_span(t_pool, 0.05, 99.95)
        y_sp = self._percentile_span(y_pool, 0.05, 99.95)
        if y_sp is None:
            return
        t_ref = self._median_channel_t_end() if self._walking else None
        use_walk_x = bool(
            self._walking and t_ref is not None and math.isfinite(t_ref)
        )
        x_eff = None if use_walk_x else x_sp
        self._apply_padded_axis_limits(
            x_span=x_eff, y_span=y_sp, walking_t_ref=t_ref, t_pool=t_pool
        )
        self._sanitize_axis_limits()
        self.refresh_pixmap()

    def _sanitize_axis_limits(self) -> None:
        mx, xx, my, xy = self.min_x, self.max_x, self.min_y, self.max_y
        if not all(math.isfinite(v) for v in (mx, xx, my, xy)):
            self.min_x, self.max_x, self.min_y, self.max_y = 0.0, 1.0, 0.0, 1.0
            return
        if xx <= mx:
            eps_x = max(1e-9, abs(mx) * 1e-15)
            self.max_x = mx + eps_x
        if xy <= my:
            eps_y = max(1e-9, abs(my) * 1e-15)
            self.max_y = my + eps_y

    def _apply_walk_or_refresh(self) -> None:
        if not self._channels:
            self.refresh_pixmap()
            return
        t_pool = self._pooled_finite("t")
        y_pool = self._pooled_finite("y")
        x_sp = self._percentile_span(t_pool, _PCT_LO, _PCT_HI)
        y_sp = self._percentile_span(y_pool, _PCT_LO, _PCT_HI)
        if y_sp is None:
            self.refresh_pixmap()
            return
        t_ref = self._median_channel_t_end() if self._walking else None
        use_walk_x = bool(
            self._walking and t_ref is not None and math.isfinite(t_ref)
        )
        x_eff = None if use_walk_x else x_sp
        self._apply_padded_axis_limits(
            x_span=x_eff, y_span=y_sp, walking_t_ref=t_ref, t_pool=t_pool
        )
        self._sanitize_axis_limits()
        self.refresh_pixmap()

    def refresh_after_data_change(self) -> None:
        self._apply_walk_or_refresh()

    def set_sliders_visible(self, on: bool) -> None:
        self.checkslider = bool(on)
        if on:
            self._layout_slider_x_from_axis()
            self._sync_slider_hit_rects()
        self.refresh_pixmap()
        self.slider_positions_changed.emit()

    def slider_data_x_positions(self) -> tuple[float | None, float | None]:
        if not self.checkslider:
            return None, None
        r = self._data_rect()
        if r.width() < 2:
            return None, None
        self._layout_slider_x_from_axis()
        if self._slider_x_a is None or self._slider_x_b is None:
            return None, None
        return float(self._slider_x_a), float(self._slider_x_b)

    def _data_rect(self) -> QRect:
        w, h = self.width(), self.height()
        ml, mr, mt, mb = self._margin_left, self._margin_right, self._margin_top, self._margin_bottom
        return QRect(ml, mt, max(0, w - ml - mr), max(0, h - mt - mb))

    def _px_to_x(self, px: int, rect: QRect) -> float:
        span = float(self.max_x - self.min_x)
        if span <= 0:
            return float(self.min_x)
        fw = float(max(rect.width() - 1, 1))
        return float(self.min_x) + (float(px) - float(rect.left())) / fw * span

    def _x_to_px(self, x_data: float, rect: QRect) -> int:
        span = float(self.max_x - self.min_x)
        if span <= 0 or not math.isfinite(span):
            return int(rect.left() + max(rect.width() - 1, 0) // 2)
        fw = float(max(rect.width() - 1, 1))
        t = (float(x_data) - float(self.min_x)) / span
        t = max(0.0, min(1.0, t))
        return int(round(float(rect.left()) + t * fw))

    def _min_slider_sep_data(self, rect: QRect) -> float:
        span = float(self.max_x - self.min_x)
        fw = float(max(rect.width() - 1, 1))
        if span <= 0 or fw <= 0 or not math.isfinite(span):
            return 0.0
        return (8.0 / fw) * span

    def _effective_slider_sep_data(self, rect: QRect) -> float:
        """Minimum A/B separation in data-x (fallback when view span is degenerate)."""
        sep = self._min_slider_sep_data(rect)
        if sep > 0.0:
            return sep
        span = abs(float(self.max_x - self.min_x))
        if math.isfinite(span) and span > 0.0:
            return max(1e-30, span * 1e-12)
        return 1e-30

    @staticmethod
    def _slider_x_within_axis_span(x_data: float, lo: float, hi: float) -> bool:
        if not math.isfinite(x_data) or not math.isfinite(lo) or not math.isfinite(hi):
            return False
        if hi < lo:
            lo, hi = hi, lo
        return lo <= x_data <= hi

    def _slider_x_visible_on_plot(self, x_data: float) -> bool:
        return self._slider_x_within_axis_span(
            x_data, float(self.min_x), float(self.max_x)
        )

    def _layout_slider_x_from_axis(self) -> None:
        """Init default positions; keep A/B ordered with min separation (may lie outside the visible x range)."""
        if not self.checkslider:
            return
        r = self._data_rect()
        if r.width() < 10:
            return
        lo = float(self.min_x)
        hi = float(self.max_x)
        span = hi - lo
        if span <= 0 or not math.isfinite(span):
            return
        sep = self._effective_slider_sep_data(r)

        if self._slider_x_a is None or self._slider_x_b is None:
            self._slider_x_a = lo + 0.35 * span
            self._slider_x_b = lo + 0.65 * span

        if float(self._slider_x_b) - float(self._slider_x_a) < sep:
            mid = 0.5 * (float(self._slider_x_a) + float(self._slider_x_b))
            self._slider_x_a = mid - 0.5 * sep
            self._slider_x_b = mid + 0.5 * sep

    def _slider_circle_rect(self, line_x: int, rect: QRect) -> QRect:
        r = _SLIDER_CIRCLE_R
        # Kreis soll die obere Plot-Grenze tangieren: Unterkante auf rect.top().
        cy = rect.top() - r
        return QRect(line_x - r, cy - r, 2 * r + 1, 2 * r + 1)

    def _slider_hit_column_rect(self, line_x: int, rect: QRect) -> QRect:
        """Widened hit area around a slider line for easier dragging."""
        r = _SLIDER_CIRCLE_R
        pad_x = 4
        top = rect.top() - 2 * r
        height = rect.height() + 2 * r
        return QRect(line_x - r - pad_x, top, 2 * r + 1 + 2 * pad_x, height)

    def _sync_slider_hit_rects(self) -> None:
        if not self.checkslider:
            self._slider_hit_a = QRect()
            self._slider_hit_b = QRect()
            return
        r = self._data_rect()
        if r.width() < 10:
            return
        self._layout_slider_x_from_axis()
        if self._slider_x_a is None or self._slider_x_b is None:
            self._slider_hit_a = QRect()
            self._slider_hit_b = QRect()
            return
        self._slider_hit_a = QRect()
        self._slider_hit_b = QRect()
        if self._slider_x_visible_on_plot(float(self._slider_x_a)):
            la = self._x_to_px(float(self._slider_x_a), r)
            self._slider_hit_a = self._slider_hit_column_rect(la, r)
        if self._slider_x_visible_on_plot(float(self._slider_x_b)):
            lb = self._x_to_px(float(self._slider_x_b), r)
            self._slider_hit_b = self._slider_hit_column_rect(lb, r)

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.refresh_pixmap()
        QTimer.singleShot(0, self.refresh_pixmap)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        if self.checkslider:
            self._layout_slider_x_from_axis()
            self._sync_slider_hit_rects()
        self.refresh_pixmap()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        p = QPainter(self)
        try:
            if not self._pixmap.isNull():
                p.drawPixmap(0, 0, self._pixmap)
            if self._rubber_active:
                pen = QPen(QColor(220, 220, 255), 1, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                rr = self._rubber_rect.normalized()
                if rr.width() >= 2 and rr.height() >= 2:
                    p.drawRect(rr.adjusted(0, 0, -1, -1))
        finally:
            p.end()

    def refresh_pixmap(self) -> None:
        w = max(1, self.width())
        h = max(1, self.height())
        # Adjust left margin so Y labels fit before drawing.
        self._update_left_margin_for_labels()
        self._pixmap = QPixmap(w, h)
        # Widget background (outside plot) stays dark gray.
        self._pixmap.fill(QColor(47, 47, 47))
        painter = QPainter(self._pixmap)
        try:
            self._draw_all(painter)
        finally:
            painter.end()
        self.update()

    def _draw_all(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self._data_rect()
        if not rect.isValid() or rect.width() < 2 or rect.height() < 2:
            return
        # Keep black only inside the actual oscilloscope plot area.
        painter.fillRect(rect, QColor(0, 0, 0))
        self._draw_grid(painter, rect)
        painter.setPen(self._pen_axis)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        self._draw_curves(painter, rect)
        if self.checkslider:
            self._layout_slider_x_from_axis()
            self._draw_sliders(painter, rect)
            self._sync_slider_hit_rects()

    def _draw_grid(self, painter: QPainter, rect: QRect) -> None:
        painter.setPen(self._pen_dash)
        span_x = float(self.max_x - self.min_x)
        span_y = float(self.max_y - self.min_y)
        if not (math.isfinite(span_x) and math.isfinite(span_y)) or span_x <= 0 or span_y <= 0:
            return
        # Compute "nice" tick values for both axes
        x0, xstep, nx = self._nice_ticks(self.min_x, self.max_x, self._num_xticks)
        y0, ystep, ny = self._nice_ticks(self.min_y, self.max_y, self._num_yticks)

        # Store main tick positions for secondary (fine) grid
        x_ticks: list[float] = []
        for i in range(nx):
            xv = x0 + i * xstep
            rx = (xv - self.min_x) / span_x
            if 0.0 <= rx <= 1.0:
                xf = rect.left() + rx * (rect.width() - 1)
                xi = int(round(xf))
                painter.drawLine(xi, rect.top(), xi, rect.bottom())
                x_ticks.append(xv)
        y_ticks: list[float] = []
        for j in range(ny):
            yv = y0 + j * ystep
            ry = (yv - self.min_y) / span_y
            if 0.0 <= ry <= 1.0:
                yf = rect.bottom() - ry * (rect.height() - 1)
                yi = int(round(yf))
                painter.drawLine(rect.left(), yi, rect.right(), yi)

                y_ticks.append(yv)

        # Secondary finer grid (dotted), no labels
        fine_pen = QPen(QColor(90, 90, 90), 1, Qt.PenStyle.DotLine)
        painter.setPen(fine_pen)
        for i in range(len(x_ticks) - 1):
            xv = 0.5 * (x_ticks[i] + x_ticks[i + 1])
            rx = (xv - self.min_x) / span_x
            if 0.0 <= rx <= 1.0:
                xf = rect.left() + rx * (rect.width() - 1)
                xi = int(round(xf))
                painter.drawLine(xi, rect.top(), xi, rect.bottom())
        for j in range(len(y_ticks) - 1):
            yv = 0.5 * (y_ticks[j] + y_ticks[j + 1])
            ry = (yv - self.min_y) / span_y
            if 0.0 <= ry <= 1.0:
                yf = rect.bottom() - ry * (rect.height() - 1)
                yi = int(round(yf))
                painter.drawLine(rect.left(), yi, rect.right(), yi)

        painter.setPen(QColor(224, 224, 224))
        painter.setFont(self.font())
        for i in range(nx):
            xv = x0 + i * xstep
            rx = (xv - self.min_x) / span_x
            if 0.0 <= rx <= 1.0:
                xf = rect.left() + rx * (rect.width() - 1)
                xi = int(round(xf))
                ax = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                painter.drawText(xi - 40, rect.bottom() + 4, 80, 18, ax, f"{xv:.6g}")
        for j in range(ny):
            yv = y0 + j * ystep
            ry = (yv - self.min_y) / span_y
            if 0.0 <= ry <= 1.0:
                yf = rect.bottom() - ry * (rect.height() - 1)
                yi = int(round(yf))
                ay = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                painter.drawText(2, yi - 10, self._margin_left - 6, 20, ay, f"{yv:.6g}")

    def _draw_sliders(self, painter: QPainter, rect: QRect) -> None:
        if self._slider_x_a is None or self._slider_x_b is None:
            return
        top_y = rect.top() + 2
        bot_y = rect.bottom() - 2
        if self._slider_x_visible_on_plot(float(self._slider_x_a)):
            la = self._x_to_px(float(self._slider_x_a), rect)
            painter.setPen(self._pen_slider_a)
            painter.drawLine(la, top_y, la, bot_y)
            ra = self._slider_circle_rect(la, rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color_slider_a)
            painter.drawEllipse(ra)
            painter.setPen(QPen(QColor(40, 30, 0), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawText(ra, Qt.AlignmentFlag.AlignCenter, "A")
        if self._slider_x_visible_on_plot(float(self._slider_x_b)):
            lb = self._x_to_px(float(self._slider_x_b), rect)
            painter.setPen(self._pen_slider_b)
            painter.drawLine(lb, top_y, lb, bot_y)
            rb = self._slider_circle_rect(lb, rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color_slider_b)
            painter.drawEllipse(rb)
            painter.setPen(QPen(QColor(60, 0, 60), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawText(rb, Qt.AlignmentFlag.AlignCenter, "B")
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_curves(self, painter: QPainter, rect: QRect) -> None:
        painter.save()
        painter.setClipRect(rect.adjusted(1, 1, -1, -1))
        span_x = float(self.max_x - self.min_x)
        span_y = float(self.max_y - self.min_y)
        if not (math.isfinite(span_x) and math.isfinite(span_y)) or span_x <= 0 or span_y <= 0:
            painter.restore()
            return
        fw = float(max(rect.width() - 1, 1))
        fh = float(max(rect.height() - 1, 1))
        mx = float(self.min_x)
        my = float(self.min_y)
        rl = float(rect.left())
        rb = float(rect.bottom())
        for ch in self._channels.values():
            if not ch.get("visible", True):
                continue
            t = ch["t"]
            y = ch["y"]
            pen = ch["pen"]
            if len(t) == 0:
                continue
            ta = np.asarray(t, dtype=np.float64).ravel()
            ya = np.asarray(y, dtype=np.float64).ravel()
            n = int(min(ta.size, ya.size))
            if n == 0:
                continue
            if n > _MAX_DRAW_POINTS:
                idx = np.linspace(0, n - 1, _MAX_DRAW_POINTS, dtype=np.int64)
                ta = ta[idx]
                ya = ya[idx]
                n = ta.size
            painter.setPen(pen)  # type: ignore[arg-type]
            poly = QPolygonF()
            for i in range(n):
                try:
                    xf = float(np.asarray(ta[i], dtype=np.float64).item())
                    yf = float(np.asarray(ya[i], dtype=np.float64).item())
                except (TypeError, ValueError):
                    if len(poly) >= 2:
                        painter.drawPolyline(poly)
                    poly = QPolygonF()
                    continue
                if not (math.isfinite(xf) and math.isfinite(yf)):
                    if len(poly) >= 2:
                        painter.drawPolyline(poly)
                    poly = QPolygonF()
                    continue
                dx = xf - mx
                dy = yf - my
                xv = rl + dx * fw / span_x
                yv = rb - dy * fh / span_y
                if not (math.isfinite(xv) and math.isfinite(yv)):
                    if len(poly) >= 2:
                        painter.drawPolyline(poly)
                    poly = QPolygonF()
                    continue
                poly.append(QPointF(xv, yv))
            if len(poly) >= 2:
                painter.drawPolyline(poly)
        painter.restore()

    def _apply_pan_pixels(self, dpx_x: int, dpx_y: int) -> None:
        rect = self._data_rect()
        if rect.width() < 2 or rect.height() < 2:
            return
        span_x = float(self.max_x - self.min_x)
        span_y = float(self.max_y - self.min_y)
        fw = float(max(rect.width() - 1, 1))
        fh = float(max(rect.height() - 1, 1))
        dx = -dpx_x / fw * span_x
        dy = dpx_y / fh * span_y
        self.min_x += dx
        self.max_x += dx
        self.min_y += dy
        self.max_y += dy
        self._sanitize_axis_limits()

    def _scroll_wheel(self, delta_y: int, *, horizontal: bool) -> None:
        rect = self._data_rect()
        if rect.width() < 2 or rect.height() < 2:
            return
        x0, xstep, _ = self._nice_ticks(self.min_x, self.max_x, self._num_xticks)
        y0, ystep, _ = self._nice_ticks(self.min_y, self.max_y, self._num_yticks)
        step_x = xstep
        step_y = ystep
        ticks = delta_y / 120.0
        if horizontal:
            self.min_x -= ticks * step_x
            self.max_x -= ticks * step_x
        else:
            self.min_y += ticks * step_y
            self.max_y += ticks * step_y
        self._sanitize_axis_limits()

    def _zoom_at_cursor(
        self,
        pos: QPoint,
        *,
        zoom_in: bool,
        mode: str,
    ) -> None:
        rect = self._data_rect()
        if rect.width() < 2 or rect.height() < 2:
            return
        fac = _ZOOM_WHEEL_FACTOR if zoom_in else 1.0 / _ZOOM_WHEEL_FACTOR
        span_x = float(self.max_x - self.min_x)
        span_y = float(self.max_y - self.min_y)
        px, py = float(pos.x()), float(pos.y())
        if mode in ("x", "both"):
            fw = float(max(rect.width() - 1, 1))
            rx = (px - float(rect.left())) / fw
            rx = min(1.0, max(0.0, rx))
            new_span = span_x * fac
            cx = self.min_x + rx * span_x
            self.min_x = cx - rx * new_span
            self.max_x = cx + (1.0 - rx) * new_span
        if mode in ("y", "both"):
            fh = float(max(rect.height() - 1, 1))
            ry = (float(rect.bottom()) - py) / fh
            ry = min(1.0, max(0.0, ry))
            new_span = span_y * fac
            cy = self.min_y + ry * span_y
            self.min_y = cy - ry * new_span
            self.max_y = cy + (1.0 - ry) * new_span
        self._sanitize_axis_limits()

    def _in_x_axis_strip(self, pos: QPoint) -> bool:
        r = self._data_rect()
        if r.height() < 4:
            return False
        return pos.y() >= r.bottom() - 2 and pos.y() <= self.height() - self._margin_bottom + _AXIS_WHEEL_STRIP_PX

    def _in_y_axis_strip(self, pos: QPoint) -> bool:
        r = self._data_rect()
        return pos.x() <= r.left() + _AXIS_WHEEL_STRIP_PX and r.top() <= pos.y() <= r.bottom()

    def _apply_rubber_zoom(self) -> None:
        wr = self._rubber_rect.normalized()
        if wr.width() < 4 or wr.height() < 4:
            return
        rect = self._data_rect()
        zr = wr.intersected(
            QRect(rect.left(), rect.top(), rect.width(), rect.height())
        )
        if zr.width() < 4 or zr.height() < 4:
            return
        span_x = float(self.max_x - self.min_x)
        span_y = float(self.max_y - self.min_y)
        fw = float(max(rect.width() - 1, 1))
        fh = float(max(rect.height() - 1, 1))
        x0 = float(self.min_x) + (float(zr.left()) - float(rect.left())) / fw * span_x
        x1 = float(self.min_x) + (float(zr.right()) - float(rect.left())) / fw * span_x
        y_top = float(self.min_y) + (float(rect.bottom()) - float(zr.top())) / fh * span_y
        y_bot = float(self.min_y) + (float(rect.bottom()) - float(zr.bottom())) / fh * span_y
        self.min_x = min(x0, x1)
        self.max_x = max(x0, x1)
        self.min_y = min(y_top, y_bot)
        self.max_y = max(y_top, y_bot)
        self._sanitize_axis_limits()
        if self.checkslider:
            self._layout_slider_x_from_axis()
            self._sync_slider_hit_rects()

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        pos = event.position().toPoint()
        dy = event.angleDelta().y()
        mods = event.modifiers()
        if dy == 0:
            event.accept()
            return
        zoom_in = dy > 0
        if (mods == Qt.KeyboardModifier.NoModifier) and self._in_y_axis_strip(pos):
            self._zoom_at_cursor(pos, zoom_in=zoom_in, mode="y")
            self.refresh_pixmap()
        elif (mods == Qt.KeyboardModifier.NoModifier) and self._in_x_axis_strip(pos):
            self._zoom_at_cursor(pos, zoom_in=zoom_in, mode="x")
            self.refresh_pixmap()
        elif mods & Qt.KeyboardModifier.ControlModifier:
            if self._in_y_axis_strip(pos):
                self._zoom_at_cursor(pos, zoom_in=zoom_in, mode="y")
            elif self._in_x_axis_strip(pos):
                self._zoom_at_cursor(pos, zoom_in=zoom_in, mode="x")
            else:
                self._zoom_at_cursor(pos, zoom_in=zoom_in, mode="both")
            self.refresh_pixmap()
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            self._scroll_wheel(dy, horizontal=True)
            self.refresh_pixmap()
        else:
            self._scroll_wheel(dy, horizontal=False)
            self.refresh_pixmap()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        pos = event.position().toPoint()
        r = self._data_rect()

        # Slider: linke Maustaste hat Priorität – wenn auf A/B geklickt wird,
        # wird NICHT gepannt.
        if self.checkslider and event.button() == Qt.MouseButton.LeftButton:
            if self._slider_hit_a.contains(pos):
                self.flag_a = False
                self.flag_b = True
                self._layout_slider_x_from_axis()
                w = max(1, self.width())
                x = max(0, min(w - 1, int(pos.x())))
                x_data = self._px_to_x(x, r)
                sep = self._effective_slider_sep_data(r)
                xb = float(self._slider_x_b) if self._slider_x_b is not None else x_data + sep
                if xb - x_data < sep:
                    x_data = xb - sep
                self._slider_x_a = x_data
                self._sync_slider_hit_rects()
                self.refresh_pixmap()
                self.slider_positions_changed.emit()
                event.accept()
                return
            if self._slider_hit_b.contains(pos):
                self.flag_b = False
                self.flag_a = True
                self._layout_slider_x_from_axis()
                w = max(1, self.width())
                x = max(0, min(w - 1, int(pos.x())))
                x_data = self._px_to_x(x, r)
                sep = self._effective_slider_sep_data(r)
                xa = float(self._slider_x_a) if self._slider_x_a is not None else x_data - sep
                if x_data - xa < sep:
                    x_data = xa + sep
                self._slider_x_b = x_data
                self._sync_slider_hit_rects()
                self.refresh_pixmap()
                self.slider_positions_changed.emit()
                event.accept()
                return

        if (
            event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton)
            and r.contains(pos)
        ):
            self._panning = True
            self._pan_last = QPoint(pos)
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton and r.contains(pos):
            self._rubber_active = True
            self._rubber_rect = QRect(pos, QSize(0, 0))
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            self.update()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton)
            and self._panning
        ):
            self._panning = False
            self._pan_last = None
            self.unsetCursor()
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton and self._rubber_active:
            self._rubber_active = False
            self.unsetCursor()
            self._apply_rubber_zoom()
            self.update()
            self.refresh_pixmap()
            self.flag_a = True
            self.flag_b = True
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            self.flag_a = True
            self.flag_b = True
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        pos = event.position().toPoint()

        if self._panning and self._pan_last is not None and (
            event.buttons()
            & (Qt.MouseButton.MiddleButton | Qt.MouseButton.LeftButton)
        ):
            d = pos - self._pan_last
            self._apply_pan_pixels(d.x(), d.y())
            self._pan_last = QPoint(pos)
            self.refresh_pixmap()
            event.accept()
            return

        if self._rubber_active and (event.buttons() & Qt.MouseButton.RightButton):
            tl = self._rubber_rect.topLeft()
            self._rubber_rect = QRect(tl, pos).normalized()
            self.update()
            event.accept()
            return

        if self.checkslider and (event.buttons() & Qt.MouseButton.LeftButton):
            r = self._data_rect()
            w = max(1, self.width())
            x = max(0, min(w - 1, int(pos.x())))
            x_data = self._px_to_x(x, r)
            sep = self._effective_slider_sep_data(r)
            if not self.flag_a:
                xb = float(self._slider_x_b) if self._slider_x_b is not None else x_data + sep
                if xb - x_data < sep:
                    x_data = xb - sep
                self._slider_x_a = x_data
                self._sync_slider_hit_rects()
                self.refresh_pixmap()
                self.slider_positions_changed.emit()
            elif not self.flag_b:
                xa = float(self._slider_x_a) if self._slider_x_a is not None else x_data - sep
                if x_data - xa < sep:
                    x_data = xa + sep
                self._slider_x_b = x_data
                self._sync_slider_hit_rects()
                self.refresh_pixmap()
                self.slider_positions_changed.emit()
            event.accept()
            return

        if (
            not self._panning
            and not self._rubber_active
            and not (self.checkslider and (event.buttons() & Qt.MouseButton.LeftButton))
        ):
            if self._in_x_axis_strip(pos):
                self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
            elif self._in_y_axis_strip(pos):
                self.setCursor(QCursor(Qt.CursorShape.SizeVerCursor))
            else:
                self.unsetCursor()

        super().mouseMoveEvent(event)
