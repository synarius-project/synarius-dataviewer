"""Neutral data access for plot widgets (pull / push friendly)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class TimeSeriesDataSource(Protocol):
    """Provides (t, y) arrays and optional unit strings by channel name."""

    def get_series(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        ...

    def channel_unit(self, name: str) -> str:
        ...


class CallableDataSource:
    """Adapter: ``resolve_series`` like the legacy DataViewer callback (+ optional units)."""

    def __init__(
        self,
        resolve_series: Callable[[str], tuple[np.ndarray, np.ndarray]],
        *,
        resolve_channel_unit: Callable[[str], str] | None = None,
    ) -> None:
        self._series = resolve_series
        self._unit = resolve_channel_unit

    def get_series(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        return self._series(name)

    def channel_unit(self, name: str) -> str:
        if self._unit is None:
            return ""
        return self._unit(name) or ""


def as_data_source(
    source: TimeSeriesDataSource | Callable[[str], tuple[np.ndarray, np.ndarray]],
    *,
    resolve_channel_unit: Callable[[str], str] | None = None,
) -> TimeSeriesDataSource:
    if isinstance(source, TimeSeriesDataSource):
        return source
    return CallableDataSource(source, resolve_channel_unit=resolve_channel_unit)
