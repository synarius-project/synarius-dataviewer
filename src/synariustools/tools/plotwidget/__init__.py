"""Qt time-series plot widget (Synarius scope + legend)."""

from synariustools.tools.plotwidget.factory import create_data_viewer
from synariustools.tools.plotwidget.modes import PlotViewerMode
from synariustools.tools.plotwidget.widget import DataViewerShell, DataViewerWidget

__all__ = [
    "PlotViewerMode",
    "create_data_viewer",
    "DataViewerShell",
    "DataViewerWidget",
]
