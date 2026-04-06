"""Factory for calibration curve / map viewer (embedded shell or bare widget)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, overload

from PySide6.QtWidgets import QWidget

from synariustools.tools.calmapwidget.data import CalibrationMapData
from synariustools.tools.calmapwidget.widget import CalibrationMapShell, CalibrationMapWidget


@overload
def create_calibration_map_viewer(
    data: CalibrationMapData,
    *,
    parent: QWidget | None = None,
    embedded: Literal[True] = True,
    on_applied_to_model: Callable[[CalibrationMapWidget], None] | None = None,
) -> CalibrationMapShell: ...


@overload
def create_calibration_map_viewer(
    data: CalibrationMapData,
    *,
    parent: QWidget | None = None,
    embedded: Literal[False],
    on_applied_to_model: Callable[[CalibrationMapWidget], None] | None = None,
) -> CalibrationMapWidget: ...


def create_calibration_map_viewer(
    data: CalibrationMapData,
    *,
    parent: QWidget | None = None,
    embedded: bool = True,
    on_applied_to_model: Callable[[CalibrationMapWidget], None] | None = None,
) -> CalibrationMapShell | CalibrationMapWidget:
    """Return toolbar+splitter shell (default) or a bare :class:`CalibrationMapWidget`."""
    if embedded:
        return CalibrationMapShell(data, parent, on_applied_to_model=on_applied_to_model)
    return CalibrationMapWidget(data, parent, on_applied_to_model=on_applied_to_model)
