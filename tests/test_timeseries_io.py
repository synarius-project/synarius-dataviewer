"""Tests for :mod:`synarius_dataviewer.io.timeseries_io` (no GUI)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from synarius_dataviewer.io.timeseries_io import TimeSeriesBundle, load_timeseries_file


def test_load_csv_numeric_time_first_column(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    p.write_text("t,a,b\n0,1,2\n0.1,3,4\n0.2,5,6\n", encoding="utf-8")
    b = load_timeseries_file(p)
    assert isinstance(b, TimeSeriesBundle)
    assert b.format == "csv"
    assert "a" in b.data.columns and "b" in b.data.columns
    ta, ya = b.get_series("a")
    assert len(ta) == 3
    assert ya[0] == 1.0


def test_channel_unit_from_metadata() -> None:
    df = pd.DataFrame({"a": [1.0]}, index=pd.Index([0.0], name="time"))
    b = TimeSeriesBundle(
        data=df,
        metadata={"channel_units": {"a": "m/s", "b": "V"}},
    )
    assert b.channel_unit("a") == "m/s"
    assert b.channel_unit("grp__a") == "m/s"


def test_get_series_object_column_mdf_like_arrays() -> None:
    """asammdf often yields object columns with one float or a short sample vector per row."""
    idx = np.array([0.0, 0.1, 0.2], dtype=np.float64)
    df = pd.DataFrame(
        {
            "s_scalar": [np.array([1.0]), np.array([2.0]), np.array([3.0])],
            "s_vec": [np.array([0.0, 1.0]), np.array([2.0]), np.array([3.0, 4.0, 5.0])],
        },
        index=idx,
    )
    df.index.name = "time"
    b = TimeSeriesBundle(data=df, format="test")
    t, y = b.get_series("s_scalar")
    assert np.allclose(y, [1.0, 2.0, 3.0])
    assert len(t) == 3
    t2, y2 = b.get_series("s_vec")
    assert len(y2) == 2 + 1 + 3
    assert np.isfinite(t2).all() and np.isfinite(y2).all()


def test_load_parquet_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "x.parquet"
    idx = pd.date_range("2020-01-01", periods=5, freq="ms")
    df = pd.DataFrame({"sig": range(5)}, index=idx)
    df.to_parquet(p)
    b = load_timeseries_file(p)
    assert b.format == "parquet"
    assert "sig" in b.data.columns
    t, y = b.get_series("sig")
    assert len(t) == 5
