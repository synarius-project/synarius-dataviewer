"""Calibration curve / map viewer (tabular + matplotlib, for ParaWiz and Studio)."""

from synariustools.tools.calmapwidget.data import (
    CalibrationMapData,
    supports_calibration_plot,
    supports_calibration_scalar_edit,
)
from synariustools.tools.calmapwidget.factory import create_calibration_map_viewer
from synariustools.tools.calmapwidget.widget import (
    CalibrationMapCompareShell,
    CalibrationMapCompareWidget,
    CalibrationMapShell,
    CalibrationMapWidget,
    create_calibration_map_compare_viewer,
    exec_scalar_calibration_edit_dialog,
)

__all__ = [
    "CalibrationMapData",
    "CalibrationMapCompareShell",
    "CalibrationMapCompareWidget",
    "CalibrationMapShell",
    "CalibrationMapWidget",
    "create_calibration_map_compare_viewer",
    "create_calibration_map_viewer",
    "exec_scalar_calibration_edit_dialog",
    "supports_calibration_plot",
    "supports_calibration_scalar_edit",
]
