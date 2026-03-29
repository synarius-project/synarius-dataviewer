"""Load measurement time series as :class:`pandas.DataFrame` with optional metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class TimeSeriesBundle:
    """Uniform container after loading CSV, Parquet, or MDF.

    ``data`` uses a numeric time index (seconds, monotonic) shared by all channels.
    """

    data: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    format: str = ""  # csv | parquet | mdf

    def channel_names(self) -> list[str]:
        return [str(c) for c in self.data.columns]

    def get_series(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        if name not in self.data.columns:
            raise KeyError(name)
        s = self.data[name]
        t_idx = self.data.index.to_numpy(dtype=np.float64, copy=False)
        t, y = _series_to_plot_xy(t_idx, s)
        mask = np.isfinite(t) & np.isfinite(y)
        return t[mask], y[mask]

    def save_metadata_json(self, path: Path | str) -> None:
        p = Path(path)
        serializable = _json_safe(self.metadata)
        p.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")

    def channel_unit(self, name: str) -> str:
        raw = self.metadata.get("channel_units")
        if not isinstance(raw, dict):
            return ""
        if name in raw and raw[name] is not None:
            return str(raw[name])
        if "__" in name:
            tail = name.rsplit("__", 1)[-1]
            if tail in raw and raw[tail] is not None:
                return str(raw[tail])
        return ""


def _cell_to_float1d(value: Any) -> np.ndarray:
    """One MDF record → 1D float samples (empty if not convertible)."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return np.array([], dtype=np.float64)
    if isinstance(value, (np.floating, np.integer)) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ):
        return np.array([float(value)], dtype=np.float64)
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (ValueError, TypeError):
        return np.array([], dtype=np.float64)
    return arr.ravel()


def _expand_object_signal(t_base: np.ndarray, series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Build (t, y) when each row may hold a scalar or a vector (typical MDF / asammdf)."""
    n = len(series)
    t_parts: list[np.ndarray] = []
    y_parts: list[np.ndarray] = []
    default_dt = 0.001
    for i in range(n):
        samples = _cell_to_float1d(series.iloc[i])
        if samples.size == 0:
            continue
        ti = float(t_base[i])
        if i > 0 and np.isfinite(float(t_base[i - 1])):
            default_dt = max(float(t_base[i] - t_base[i - 1]), 1e-9)
        if samples.size == 1:
            t_parts.append(np.array([ti], dtype=np.float64))
            y_parts.append(samples)
            continue
        if i + 1 < n and np.isfinite(float(t_base[i + 1])):
            t_next = float(t_base[i + 1])
            span = t_next - ti
            if span > 1e-12:
                t_sample = np.linspace(ti, t_next, samples.size, endpoint=False, dtype=np.float64)
            else:
                t_sample = ti + np.arange(samples.size, dtype=np.float64) * (default_dt / samples.size)
        else:
            t_sample = ti + np.arange(samples.size, dtype=np.float64) * (default_dt / samples.size)
        t_parts.append(t_sample)
        y_parts.append(samples)
    if not t_parts:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    return np.concatenate(t_parts), np.concatenate(y_parts)


def _series_to_plot_xy(t_index: np.ndarray, series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Return aligned (t, y) for plotting; handles numeric columns and MDF-style sequence cells."""
    t = np.asarray(t_index, dtype=np.float64)
    if pd.api.types.is_object_dtype(series.dtype):
        return _expand_object_signal(t, series)
    for i in range(min(len(series), 32)):
        v = series.iloc[i]
        if isinstance(v, (list, tuple, np.ndarray, memoryview)):
            return _expand_object_signal(t, series)
    try:
        y = pd.to_numeric(series, errors="coerce").to_numpy(dtype=np.float64, copy=True)
    except (ValueError, TypeError):
        return _expand_object_signal(t, series)
    if y.size and not np.any(np.isfinite(y)):
        return _expand_object_signal(t, series)
    return t, y


def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return str(obj)


def _normalize_time_index(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure a Float64Index named *time* (seconds)."""
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        out = df.copy()
        delta = idx - idx[0]
        sec = delta.total_seconds()
        if hasattr(sec, "to_numpy"):
            sec = sec.to_numpy(dtype=np.float64)
        else:
            sec = np.asarray(sec, dtype=np.float64)
        out.index = sec
        out.index.name = "time"
        return out
    if pd.api.types.is_numeric_dtype(idx):
        out = df.copy()
        out.index = pd.Index(idx.astype(np.float64), name="time")
        return out
    # Fallback: sample index
    out = df.copy()
    out.index = pd.Index(np.arange(len(out), dtype=np.float64) * 0.001, name="time")
    return out


def load_timeseries_file(path: Path | str) -> TimeSeriesBundle:
    """Dispatch by suffix: ``.csv``, ``.parquet``/``.pq``, ``.mf4``/``.mdf``/… ."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    suf = p.suffix.lower()
    if suf == ".csv":
        return _load_csv(p)
    if suf in (".parquet", ".pq"):
        return _load_parquet(p)
    if suf in (".mf4", ".mdf", ".dat"):
        return _load_mdf(p)
    raise ValueError(f"Unsupported file type: {suf!r} ({p})")


def _load_csv(p: Path) -> TimeSeriesBundle:
    df = pd.read_csv(p)
    meta: dict[str, Any] = {"path": str(p), "columns": list(df.columns)}
    if len(df.columns) == 0:
        raise ValueError("CSV has no columns")
    first = df.columns[0]
    t = pd.to_numeric(df[first], errors="coerce")
    if t.notna().sum() >= max(1, len(df) // 2):
        mask = t.notna()
        data = df.loc[mask].drop(columns=[first]).copy()
        data.index = t.loc[mask].astype(np.float64)
        data.index.name = "time"
    else:
        data = df.copy()
        data.index = pd.Index(np.arange(len(data), dtype=np.float64) * 0.001, name="time")
    for c in data.columns:
        data[c] = pd.to_numeric(data[c], errors="coerce")
    data = data.sort_index()
    bundle = TimeSeriesBundle(data=data, metadata=meta, source_path=str(p), format="csv")
    bundle.metadata["frame_dtypes"] = {str(c): str(data[c].dtype) for c in data.columns}
    return bundle


def _load_parquet(p: Path) -> TimeSeriesBundle:
    df = pd.read_parquet(p)
    meta: dict[str, Any] = {"path": str(p)}
    try:
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(p)
        meta["parquet_schema"] = str(pf.schema_arrow)
    except Exception as exc:
        meta["parquet_schema_note"] = f"could not read sidecar schema: {exc}"
    data = _normalize_time_index(df)
    bundle = TimeSeriesBundle(data=data, metadata=meta, source_path=str(p), format="parquet")
    return bundle


def _header_to_meta(header: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in (
        "comment",
        "author",
        "department",
        "project",
        "subject",
        "application",
        "measurement_start_time",
    ):
        if hasattr(header, attr):
            v = getattr(header, attr)
            if v is not None:
                try:
                    out[attr] = v.isoformat() if hasattr(v, "isoformat") else str(v)
                except Exception:
                    out[attr] = str(v)
    return out


def _load_mdf(p: Path) -> TimeSeriesBundle:
    from asammdf import MDF

    mdf = MDF(str(p))
    try:
        meta: dict[str, Any] = {"path": str(p)}
        try:
            meta["mdf_header"] = _header_to_meta(mdf.header)
        except Exception as exc:
            meta["mdf_header_note"] = str(exc)

        df = mdf.to_dataframe(time_from_zero=True)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = ["__".join(str(part) for part in col).strip("_") for col in df.columns]

        try:
            units_map: dict[str, str] = {}
            for grp in mdf.groups:
                for ch in grp.channels:
                    nm = (getattr(ch, "name", None) or "").strip()
                    if not nm:
                        continue
                    u = (getattr(ch, "unit", None) or "").strip()
                    if u and nm not in units_map:
                        units_map[nm] = u
            if units_map:
                meta["channel_units"] = units_map
        except Exception as exc:
            meta["channel_units_note"] = str(exc)

        data = _normalize_time_index(df)
        return TimeSeriesBundle(data=data, metadata=meta, source_path=str(p), format="mdf")
    finally:
        mdf.close()
