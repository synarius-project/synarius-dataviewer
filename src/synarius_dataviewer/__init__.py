"""Data viewing utilities for Synarius."""

from synarius_dataviewer._version import __version__
from synarius_dataviewer.io import TimeSeriesBundle, load_timeseries_file

__all__: list[str] = ["__version__", "TimeSeriesBundle", "load_timeseries_file"]
