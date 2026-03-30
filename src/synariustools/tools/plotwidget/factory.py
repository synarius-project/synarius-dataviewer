"""Matplotlib-style convenience constructor for the data plot widget."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import numpy as np
from PySide6.QtWidgets import QWidget

from synariustools.tools.plotwidget.datasource import TimeSeriesDataSource
from synariustools.tools.plotwidget.modes import PlotViewerMode
from synariustools.tools.plotwidget.widget import DataViewerShell, DataViewerWidget


def create_data_viewer(
    data_source: TimeSeriesDataSource | Callable[[str], tuple[np.ndarray, np.ndarray]],
    *,
    parent: QWidget | None = None,
    embedded: bool = True,
    enable_walking_axis: bool = False,
    resolve_channel_unit: Callable[[str], str] | None = None,
    mode: PlotViewerMode | Literal["static", "dynamic"] = "static",
    legend_visible_at_start: bool | None = None,
) -> DataViewerShell | DataViewerWidget:
    """Return a toolbar+plot shell (embedded) or a bare widget for host layouts.

    * ``embedded=True`` — same layout as the Synarius Dataviewer MDI child (default).
    * ``embedded=False`` — only :class:`DataViewerWidget`, for custom window chrome.
    """
    if embedded:
        return DataViewerShell(
            data_source,
            parent,
            enable_walking_axis=enable_walking_axis,
            resolve_channel_unit=resolve_channel_unit,
            mode=mode,
            legend_visible_at_start=legend_visible_at_start,
        )
    return DataViewerWidget(
        data_source,
        parent,
        enable_walking_axis=enable_walking_axis,
        resolve_channel_unit=resolve_channel_unit,
        mode=mode,
        legend_visible_at_start=legend_visible_at_start,
    )
