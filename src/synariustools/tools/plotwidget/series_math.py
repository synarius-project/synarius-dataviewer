"""Pure NumPy helpers for scope data (interpolation, append, formatting)."""

from __future__ import annotations

import numpy as np


def fmt_measure(v: float | None) -> str:
    if v is None or not np.isfinite(v):
        return "—"
    return f"{v:.6g}"


def latest_y(ty: np.ndarray) -> float | None:
    y = np.asarray(ty, dtype=np.float64).ravel()
    if y.size == 0:
        return None
    return float(y[-1])


def interp_y_at_x(tx: np.ndarray, ty: np.ndarray, xq: float) -> float | None:
    if len(tx) == 0:
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


def append_merge(
    tx: np.ndarray | None,
    ty: np.ndarray | None,
    t_new: np.ndarray,
    y_new: np.ndarray,
    *,
    max_points: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Concatenate and trim tail to ``max_points`` (same semantics as previous widget)."""
    t_new = np.asarray(t_new, dtype=np.float64).ravel()
    y_new = np.asarray(y_new, dtype=np.float64).ravel()
    if tx is None or ty is None or len(tx) == 0:
        t_all, y_all = t_new, y_new
    else:
        t_all = np.concatenate([tx, t_new])
        y_all = np.concatenate([ty, y_new])
    if len(t_all) > max_points:
        cut = len(t_all) - max_points
        t_all = t_all[cut:]
        y_all = y_all[cut:]
    return t_all, y_all
