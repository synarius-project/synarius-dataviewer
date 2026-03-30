"""Shim: time-series I/O lives in :mod:`synarius_core.io`."""

from synarius_core.io import TimeSeriesBundle, load_timeseries_file

__all__ = ["TimeSeriesBundle", "load_timeseries_file"]
